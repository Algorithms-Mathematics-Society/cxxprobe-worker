"""Talking to ams-api: register, heartbeat, and hand back results.

This is the only place the worker knows AMS exists. With `base_url` unset it
degrades to standalone mode — judge, store artifacts, report to nobody —
which is what keeps a purely local deployment working with no AMS at all.

Translating a cxxprobe `JudgeReport` into a verdict happens here rather than
in ams-api, because the report's shape is cxxprobe's business and the worker
is already the component that owns that coupling.
"""

from __future__ import annotations

from typing import Any

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


class ControlPlaneError(RuntimeError):
    pass


def worst_verdict(verdicts: list[str]) -> str:
    if not verdicts:
        return "SE"
    return max(verdicts, key=lambda v: _VERDICT_RANK.get(v, 0))


def summarise(report: dict[str, Any] | None, error: str = "") -> dict[str, Any]:
    """Reduce a cxxprobe JudgeReport to what AMS records.

    A submission that would not compile is **CE**, not a failed job — the
    judging pipeline worked exactly as intended and produced a real verdict.
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

    manual = ((report.get("tests") or {}).get("manual")) or {}
    cases = manual.get("cases") or []

    testcases: list[dict[str, Any]] = []
    verdicts: list[str] = []
    max_ms = 0
    max_kb = 0
    for i, c in enumerate(cases):
        verdict = str(c.get("verdict") or "")
        if verdict:
            verdicts.append(verdict)
        runtime = int(c.get("wall_time_ms") or c.get("cpu_time_ms") or 0)
        memory_kb = int(c.get("peak_memory_bytes") or 0) // 1024
        max_ms = max(max_ms, runtime)
        max_kb = max(max_kb, memory_kb)
        testcases.append(
            {
                "testcase_no": i + 1,
                "label": str(c.get("label") or ""),
                "verdict": verdict or "SE",
                "runtime_ms": runtime,
                "memory_kb": memory_kb,
                "exit_code": int(c.get("exit_code") or 0),
                "checker_message": str(c.get("checker_diagnostics") or ""),
            }
        )

    return {
        "verdict": worst_verdict(verdicts),
        "passed_count": int(manual.get("passed") or 0),
        "total_count": int(manual.get("total") or 0),
        "max_runtime_ms": max_ms,
        "max_memory_kb": max_kb,
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
