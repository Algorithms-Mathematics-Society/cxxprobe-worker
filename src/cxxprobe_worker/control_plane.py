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

    def register(self, hostname: str, version: str = "") -> str | None: ...

    def publish_result(self, result: JobResult) -> bool: ...


class ControlPlaneError(RuntimeError):
    pass


def worst_verdict(verdicts: list[str]) -> str:
    if not verdicts:
        return "SE"
    return max(verdicts, key=lambda v: _VERDICT_RANK.get(v, 0))


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
        cases.append(
            {
                "kind": "symbolic",
                "testcase_no": offset + i + 1,
                "label": f"{kind}: {pattern}"[:64],
                "verdict": verdict,
                "runtime_ms": 0,
                "memory_kb": 0,
                "exit_code": 0,
                "checker_message": str(c.get("message") or ""),
            }
        )
    return cases, verdicts


def summarise(report: dict[str, Any] | None, error: str = "") -> dict[str, Any]:
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

    compile_section = report.get("compile") or {}
    solution = compile_section.get("solution") or {}
    compile_output = str(solution.get("diagnostics") or "")

    if solution.get("ok") is False:
        return {
            "verdict": "CE",
            "passed_count": 0,
            "total_count": 0,
            "max_runtime_ms": 0,
            "max_memory_kb": 0,
            "compile_output": compile_output[:20000],
            "testcases": [],
            "error": "",
        }

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

        summary = summarise(result.report, result.error or "")
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
