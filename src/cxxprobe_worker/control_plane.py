"""Talking to ams-api: register, heartbeat, and hand back results.

This is the only place the worker knows AMS exists. With `base_url` unset it
degrades to standalone mode — judge, store artifacts, report to nobody —
which is what keeps a purely local deployment working with no AMS at all.

Translating a cxxprobe `JudgeReport` into a verdict happens here rather than
in ams-api, because the report's shape is cxxprobe's business and the worker
is already the component that owns that coupling.
"""

from __future__ import annotations

from typing import Any, Protocol

from cxxprobe_worker.config import ControlPlaneConfig
from cxxprobe_worker.executor import _diagnostics_from, _submission_failed_to_compile
from cxxprobe_worker.jobs import JobResult, JobStatus
from cxxprobe_worker.monitoring import Logger

# cxxprobe's own precedence (TLE > MLE > OLE > RE > WA > AC), with the states
# cxxprobe cannot emit ranked above everything it can.
_VERDICT_RANK = {
    "AC": 0,
    "WA": 1,
    "RE": 2,
    "OLE": 3,
    "MLE": 4,
    "TLE": 5,
    "CE": 6,
    "SE": 7,
}


class IControlPlane(Protocol):
    """The seam between the worker and whatever records its verdicts.

    Same pattern as ``IArtifactStorage`` and ``IJobQueue``: the daemon depends
    on this, not on the HTTP client, so a deployment can substitute a
    different destination and a test can substitute a recorder.
    """

    @property
    def enabled(self) -> bool: ...

    @property
    def registered(self) -> bool:
        """Whether this worker has a control-plane identity yet."""
        ...

    def register(self, hostname: str, version: str = "") -> str | None: ...

    def heartbeat(
        self, running_jobs: int = 0, cpu_percent: int = 0, memory_percent: int = 0
    ) -> None:
        """Say this worker is alive.

        On the protocol deliberately. It was implemented on the client and
        reachable on the server for months, but absent here — so the daemon
        had no way to call it and never did, and every worker showed as
        offline no matter how much it was judging.
        """
        ...

    def publish_result(self, result: JobResult) -> bool: ...


class ControlPlaneError(RuntimeError):
    pass


def worst_verdict(verdicts: list[str]) -> str:
    if not verdicts:
        return "SE"
    return max(verdicts, key=lambda v: _VERDICT_RANK.get(v, 0))


def _custom_cases(report: dict[str, Any]) -> dict[str, Any]:
    """Reduce a `judge --custom-cases` report to what AMS records.

    A different report shape from the ordinary one: cxxprobe emits
    `{status, compile, cases}` with no `tests` section, because the problem's
    own three checks never ran. Falling through the normal path found no
    manual/behavior/symbolic section, collected no verdicts, and reported the
    whole thing as System Error -- a run that worked perfectly, described as
    a platform failure.

    The verdict is the worst of the *judged* cases only. A case the candidate
    gave no expected output for has no verdict to be worse than, and counting
    it would turn "show me what this prints" into a failure.
    """
    cases: list[dict[str, Any]] = []
    verdicts: list[str] = []
    for i, c in enumerate(report.get("cases") or []):
        verdict = str(c.get("verdict") or "")
        if verdict:
            verdicts.append(verdict)
        cases.append(
            {
                "kind": "io",
                "testcase_no": i + 1,
                "label": str(c.get("label") or f"Case {i + 1}"),
                # Empty, not "SE": an unjudged case is not a failed one.
                # (The API used to tell custom runs apart by stdout being
                # present here and absent above. It no longer does -- it reads
                # the job it enqueued -- because that made adding output to
                # ordinary cases leak the hidden ones.)
                "verdict": verdict,
                "runtime_ms": int(c.get("wall_time_ms") or c.get("cpu_time_ms") or 0),
                "memory_kb": int(c.get("peak_memory_bytes") or 0) // 1024,
                "exit_code": int(c.get("exit_code") or 0),
                "checker_message": str(c.get("checker_diagnostics") or ""),
                "stdout": str(c.get("stdout") or ""),
                "stderr": str(c.get("stderr") or ""),
            }
        )

    compile_section = report.get("compile") or {}
    solution = compile_section.get("solution") or {}
    compile_output = str(solution.get("diagnostics") or "")

    return {
        # Nothing judged is not a failure -- it is the ordinary "just run it"
        # case, and AC is what lets the UI show the output without a verdict.
        "verdict": worst_verdict(verdicts) if verdicts else "AC",
        "passed_count": sum(1 for c in cases if c["verdict"] == "AC"),
        "total_count": sum(1 for c in cases if c["verdict"]),
        "max_runtime_ms": max((c["runtime_ms"] for c in cases), default=0),
        "max_memory_kb": max((c["memory_kb"] for c in cases), default=0),
        "compile_output": compile_output[:20000],
        "testcases": cases,
        "error": "",
    }


def _manual_cases(section: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """I/O testcases: run the binary, compare output."""
    cases: list[dict[str, Any]] = []
    verdicts: list[str] = []
    for i, c in enumerate(section.get("cases") or []):
        verdict = str(c.get("verdict") or "")
        if verdict:
            verdicts.append(verdict)
        cases.append(
            {
                "kind": "io",
                "testcase_no": i + 1,
                "label": str(c.get("label") or i + 1),
                "verdict": verdict or "SE",
                "runtime_ms": int(c.get("wall_time_ms") or c.get("cpu_time_ms") or 0),
                "memory_kb": int(c.get("peak_memory_bytes") or 0) // 1024,
                "exit_code": int(c.get("exit_code") or 0),
                "checker_message": str(c.get("checker_diagnostics") or ""),
                # What the program actually printed.
                #
                # Omitted here for a long time while `_custom_cases` sent it,
                # so a candidate running the problem's own samples saw a
                # verdict and nothing else: of 1,216 submitted I/O cases in
                # the 24 Oct contest, not one carried any output, and anyone
                # whose answer was wrong had no way to see how.
                #
                # Safe because the API drops every non-sample case before a
                # candidate sees it, and a sample's input and expected output
                # are printed in the statement they are already reading. The
                # only thing missing was their own side of it.
                # Older engines omit these fields. Preserve that distinction
                # from an explicitly empty output emitted by cxxprobe 1.0.0.
                **{key: str(c[key]) for key in ("stdout", "stderr") if c.get(key) is not None},
            }
        )
    return cases, verdicts


def _behavior_cases(section: dict[str, Any], offset: int) -> tuple[list[dict[str, Any]], list[str]]:
    """GTest cases compiled against the submission's internal API.

    A behaviour-checked problem has *no* I/O tests at all — b-pokemon-raii
    ships an empty tests/ — so reading only the manual section reports a
    perfectly good submission as having no results.
    """
    cases: list[dict[str, Any]] = []
    verdicts: list[str] = []
    for i, c in enumerate(section.get("cases") or []):
        failed = bool(c.get("failed"))
        verdict = "WA" if failed else "AC"
        verdicts.append(verdict)
        messages = c.get("failure_messages") or []
        cases.append(
            {
                "kind": "behavior",
                "testcase_no": offset + i + 1,
                "label": str(c.get("name") or f"case {i + 1}"),
                "verdict": verdict,
                "runtime_ms": int(c.get("time_ms") or 0),
                "memory_kb": 0,
                "exit_code": 0,
                "checker_message": "\n".join(str(m) for m in messages)[:4000],
            }
        )
    return cases, verdicts


def _symbolic_cases(section: dict[str, Any], offset: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Source-level rules: `must_include` / `must_not_include`.

    These are how a-beet-cast requires std::bit_cast and rejects memcpy. A
    violated rule is a wrong answer about *how* the problem was solved, so it
    counts as WA rather than being reported only in prose.
    """
    cases: list[dict[str, Any]] = []
    verdicts: list[str] = []
    for i, c in enumerate(section.get("checks") or []):
        satisfied = bool(c.get("satisfied"))
        verdict = "AC" if satisfied else "WA"
        verdicts.append(verdict)
        kind = str(c.get("kind") or "rule")
        pattern = str(c.get("pattern") or "")
        message = str(c.get("message") or "")
        # The setter's sentence is the label when there is one.
        #
        # This label is what the contest room prints on the tab for a broken
        # rule, and `must_include: \b(std::)?istringstream\b` reached
        # candidates verbatim -- rendered as "Must use (std::)?istrings...",
        # and for an alternation the client could not clean, as a bare
        # "Must not use" that names nothing at all.
        #
        # Fixed here rather than in the client on purpose: the proctor bundles
        # its UI (tauri frontendDist), so a client-side fix reaches nobody who
        # has already installed it. The worker is deployed, so this reaches
        # everyone -- including the people who downloaded yesterday.
        cases.append(
            {
                "kind": "symbolic",
                "testcase_no": offset + i + 1,
                "label": (message or f"{kind}: {pattern}")[:64],
                "verdict": verdict,
                "runtime_ms": 0,
                "memory_kb": 0,
                "exit_code": 0,
                "checker_message": str(c.get("message") or ""),
            }
        )
    return cases, verdicts


def _section_status(section: Any) -> str:
    """A check's own status word, or ``SKIPPED`` when the part has no such check."""
    if not isinstance(section, dict):
        return "SKIPPED"
    return str(section.get("status") or "SKIPPED").upper()


def _integration_verdict(integration: dict[str, Any]) -> str | None:
    """The integration check as a verdict, or ``None`` when it did not run.

    ``ERROR`` is cxxprobe's word for a compile or link failure as well as for
    a machine fault; the compile step's exit code tells them apart, exactly as
    it does for a single submission.
    """
    status = str(integration.get("status") or "").upper()
    if status in {"", "SKIPPED"}:
        return None
    if status == "PASS":
        return "AC"
    if status == "FAIL":
        return "WA"
    step = integration.get("compile")
    if isinstance(step, dict) and step.get("ok") is False:
        exit_code = step.get("exit_code")
        if isinstance(exit_code, int) and exit_code >= 0:
            return "CE"
    return "SE"


def _project(report: dict[str, Any]) -> dict[str, Any]:
    """Reduce a `cxxprobe test project` report to what AMS records.

    Selection in a team round is individual, so the one thing this must not
    do is flatten: a person can only be judged on their own part. Each part
    keeps its own verdict and per-check status; the integration result stays
    separate, because "my part passes alone" and "the parts do not work
    together" are two different facts and the consumer needs both.

    The verdicts are cxxprobe's. Each part's block is an ordinary judge report
    and goes through the same `summarise` as any submission; nothing here
    re-decides whether a part passed.

    The flat fields (`verdict`, `testcases`, ...) are still filled, from every
    part's cases tagged with their label, so a consumer that knows nothing of
    projects records a sensible row rather than an empty one.
    """
    substitutions = [
        {"label": str(s.get("label")), "with": str(s.get("with") or s.get("fallback") or "")}
        for s in report.get("substitutions") or []
        if isinstance(s, dict) and s.get("label") is not None
    ]
    substituted = {s["label"]: s["with"] for s in substitutions}

    parts: dict[str, dict[str, Any]] = {}
    testcases: list[dict[str, Any]] = []
    verdicts: list[str] = []
    compile_output: list[str] = []
    raw_parts = report.get("parts") or {}
    for label, part in sorted(raw_parts.items()):
        if not isinstance(part, dict):
            continue
        summary = summarise(part)
        tests = part.get("tests") or {}
        parts[label] = {
            "verdict": summary["verdict"],
            "manual": _section_status(tests.get("manual")),
            "behavior": _section_status(tests.get("behavior")),
            "symbolic": _section_status(tests.get("symbolic")),
            "passed_count": summary["passed_count"],
            "total_count": summary["total_count"],
            "compile_output": summary["compile_output"],
            # A substitution must never be invisible to whoever reads this.
            "substituted": substituted.get(label),
        }
        verdicts.append(summary["verdict"])
        if summary["compile_output"]:
            compile_output.append(f"[part {label}]\n{summary['compile_output']}")
        for case in summary["testcases"]:
            testcases.append({**case, "part": label, "testcase_no": len(testcases) + 1})

    raw_integration = report.get("integration")
    integration_in = raw_integration if isinstance(raw_integration, dict) else {}
    integration_verdict = _integration_verdict(integration_in)
    # A failing integration case is pinned on a part when cxxprobe names one;
    # the rest belongs to the project as a whole, not to whoever is listed
    # first.
    attributed: dict[str, list[str]] = {}
    unattributed: list[str] = []
    for case in integration_in.get("cases") or []:
        if not isinstance(case, dict) or not case.get("failed"):
            continue
        name = str(case.get("name") or "")
        owner = case.get("part")
        if owner is not None and str(owner) in parts:
            attributed.setdefault(str(owner), []).append(name)
        else:
            unattributed.append(name)
    integration_compile = integration_in.get("compile")
    integration_diagnostics = (
        str(integration_compile.get("diagnostics") or "")
        if isinstance(integration_compile, dict)
        else ""
    )
    integration = {
        "status": str(integration_in.get("status") or "SKIPPED").upper(),
        "verdict": integration_verdict,
        "failed_parts": attributed,
        "unattributed": unattributed,
        "diagnostics": integration_diagnostics[:20000],
    }
    if integration_verdict is not None:
        verdicts.append(integration_verdict)
    if integration_diagnostics:
        compile_output.append(f"[integration]\n{integration_diagnostics}")

    failing_parts = sorted(
        {label for label, part in parts.items() if part["verdict"] != "AC"} | set(attributed)
    )

    return {
        "verdict": worst_verdict(verdicts),
        "passed_count": sum(1 for c in testcases if c["verdict"] == "AC"),
        "total_count": len(testcases),
        "max_runtime_ms": max((c["runtime_ms"] for c in testcases), default=0),
        "max_memory_kb": max((c["memory_kb"] for c in testcases), default=0),
        "compile_output": "\n\n".join(compile_output)[:20000],
        "testcases": testcases,
        "error": "",
        "project": str(report.get("project") or ""),
        "parts": parts,
        "integration": integration,
        "substitutions": substitutions,
        "failing_parts": failing_parts,
        # Set when the project failed but no single part can be blamed.
        "project_failure": bool(
            integration_verdict not in {None, "AC"} and not attributed and not failing_parts
        ),
    }


def _killed_judge(signal_name: str) -> dict[str, Any]:
    """A submission that killed the judge: a runtime error, with its cause.

    Sent with no ``error`` on purpose. ams-api treats any error as "this
    machine could not judge" and requeues, which is exactly the loop that
    spent five attempts on each of these and then reported nothing.
    """
    message = f"The judge was killed by {signal_name} while running this submission."
    return {
        "verdict": "RE",
        "passed_count": 0,
        "total_count": 1,
        "max_runtime_ms": 0,
        "max_memory_kb": 0,
        "compile_output": "",
        "testcases": [
            {
                "kind": "io",
                "testcase_no": 1,
                "label": "Judge run",
                "verdict": "RE",
                "runtime_ms": 0,
                "memory_kb": 0,
                "exit_code": 0,
                "checker_message": message,
            }
        ],
        "error": "",
        "judge_signal": signal_name,
    }


def summarise(
    report: dict[str, Any] | None, error: str = "", *, judge_signal: str | None = None
) -> dict[str, Any]:
    """Reduce a cxxprobe JudgeReport to what AMS records.

    cxxprobe checks three independent things, and a problem may use any
    combination:

    * **manual** — I/O testcases
    * **behavior** — GTest cases compiled against the submission's API
    * **symbolic** — source rules (`must_include` / `must_not_include`)

    Reading only `manual` reported every behaviour-checked submission as
    System Error: b-pokemon-raii has an empty tests/ by design, so a passing
    6/6 gtest run looked like a job that produced no result at all.

    A submission that would not compile is **CE**, not a failed job — the
    pipeline worked exactly as intended and produced a real verdict.
    """
    if judge_signal is not None:
        return _killed_judge(judge_signal)

    if report is None:
        return {
            "verdict": "SE",
            "passed_count": 0,
            "total_count": 0,
            "max_runtime_ms": 0,
            "max_memory_kb": 0,
            "compile_output": "",
            "testcases": [],
            "error": error or "no report produced",
        }

    # A project report has no top-level compile or tests: everything lives
    # under its parts and its integration block.
    if isinstance(report.get("parts"), dict):
        return _project(report)

    compile_section = report.get("compile") or {}
    solution = compile_section.get("solution") or {}
    # Warnings from a build that succeeded are still worth showing.
    compile_output = str(solution.get("diagnostics") or "")

    # CE only when the compiler actually ran and rejected the submission, in
    # any step that builds it. A GTest-only problem has no `solution` step,
    # and a step that never launched is the machine's fault, not a CE.
    if _submission_failed_to_compile(report):
        return {
            "verdict": "CE",
            "passed_count": 0,
            "total_count": 0,
            "max_runtime_ms": 0,
            "max_memory_kb": 0,
            "compile_output": (_diagnostics_from(report) or compile_output)[:20000],
            "testcases": [],
            "error": "",
        }

    # A custom run carries `cases` at the top level and no `tests` section.
    # Checked after the compile branch above, so a custom run that will not
    # build still reports CE with its diagnostics rather than an empty list.
    if "tests" not in report and "cases" in report:
        return _custom_cases(report)

    tests = report.get("tests") or {}
    testcases: list[dict[str, Any]] = []
    verdicts: list[str] = []

    manual, manual_verdicts = _manual_cases(tests.get("manual") or {})
    testcases += manual
    verdicts += manual_verdicts

    behavior, behavior_verdicts = _behavior_cases(tests.get("behavior") or {}, len(testcases))
    testcases += behavior
    verdicts += behavior_verdicts

    symbolic, symbolic_verdicts = _symbolic_cases(tests.get("symbolic") or {}, len(testcases))
    testcases += symbolic
    verdicts += symbolic_verdicts

    passed = sum(1 for c in testcases if c["verdict"] == "AC")

    # A section that errored produced no cases, so it contributes no verdict
    # — but it is emphatically not a pass. Everything compiled, so the run
    # died on the submission's own code: a crashed GTest binary is RE.
    crashed = any(
        isinstance(section, dict) and str(section.get("status", "")).upper() == "ERROR"
        for section in tests.values()
    )

    if crashed:
        verdict = "RE"
    elif verdicts:
        verdict = worst_verdict(verdicts)
    else:
        # Nothing ran at all. `overall` is cxxprobe's own word for it, and
        # ERROR there means the judge could not do its job — not that the
        # submission was wrong.
        overall = str(report.get("overall") or "").upper()
        verdict = "AC" if overall == "PASS" else "SE"

    return {
        "verdict": verdict,
        "passed_count": passed,
        "total_count": len(testcases),
        "max_runtime_ms": max((c["runtime_ms"] for c in testcases), default=0),
        "max_memory_kb": max((c["memory_kb"] for c in testcases), default=0),
        "compile_output": compile_output[:20000],
        "testcases": testcases,
        "error": "",
    }


class ControlPlaneClient:
    """HTTP client for ams-api's worker endpoints."""

    def __init__(self, config: ControlPlaneConfig, logger: Logger) -> None:
        self._config = config
        self._log = logger
        self._worker_uid: str | None = None
        self._client: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self._config.base_url)

    @property
    def worker_uid(self) -> str | None:
        return self._worker_uid

    def _http(self) -> Any:
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover
                raise ControlPlaneError(
                    "the control plane needs httpx — install with `uv sync --extra aws`"
                ) from exc
            self._client = httpx.Client(
                base_url=self._config.base_url.rstrip("/"),
                timeout=self._config.timeout_seconds,
                headers={"Authorization": f"Bearer {self._config.api_key}"},
            )
        return self._client

    @property
    def registered(self) -> bool:
        """Whether this worker has a control-plane identity yet.

        Without one `heartbeat` silently no-ops, so a registration that
        failed at startup leaves the worker invisible while it keeps
        judging — which is why the daemon retries on this.
        """
        return bool(self._worker_uid)

    def register(self, hostname: str, version: str = "") -> str | None:
        if not self.enabled:
            return None
        try:
            resp = self._http().post(
                "/workers/register",
                json={"hostname": hostname, "version": version, "pool": self._config.pool},
            )
            resp.raise_for_status()
            self._worker_uid = str(resp.json()["worker_uid"])
        except Exception as exc:
            # Registration failing must not stop the worker judging. Jobs are
            # claimed from the queue, not handed out by ams-api, so the only
            # loss is visibility.
            self._log.warning("control_plane.register_failed", error=str(exc))
            return None
        self._log.info("control_plane.registered", worker_uid=self._worker_uid)
        return self._worker_uid

    def heartbeat(
        self, running_jobs: int = 0, cpu_percent: int = 0, memory_percent: int = 0
    ) -> None:
        if not self.enabled or not self._worker_uid:
            return
        try:
            self._http().post(
                "/workers/heartbeat",
                json={
                    "worker_uid": self._worker_uid,
                    "running_jobs": running_jobs,
                    "cpu_percent": cpu_percent,
                    "memory_percent": memory_percent,
                },
            )
        except Exception as exc:
            self._log.debug("control_plane.heartbeat_failed", error=str(exc))

    def publish_result(self, result: JobResult) -> bool:
        """Hand a finished job's verdict back to AMS.

        Returns False when the result could not be delivered, which the
        caller must treat as "do not delete the queue message" — otherwise
        the submission is judged but its verdict is lost for ever.
        """
        if not self.enabled:
            return True

        summary = summarise(result.report, result.error or "", judge_signal=result.judge_signal)
        if result.status is JobStatus.RETRYABLE:
            # Nothing to report: the job did not produce a verdict and will
            # be redelivered.
            return True

        payload = {
            "job_uid": result.job_id,
            "worker_uid": self._worker_uid,
            **summary,
        }
        try:
            resp = self._http().post("/workers/result", json=payload)
            resp.raise_for_status()
        except Exception as exc:
            self._log.error("control_plane.result_failed", job_id=result.job_id, error=str(exc))
            return False
        self._log.info(
            "control_plane.result_sent", job_id=result.job_id, verdict=summary["verdict"]
        )
        return True

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
