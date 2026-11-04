"""Benchmark jobs: the rules that make a timing worth recording.

A benchmark job is not a judging job that happens to be slow. Its result is
only worth anything if it came from an uncontended, calibrated machine, from
the exact code that passed verification on the benchmark input, and from the
newest revision its team submitted. Everything here enforces one of those.

Coordination between benchers uses small markers in the shared artifact
storage, under the reserved ``_bench`` namespace, because that is the one
thing every worker in a pool already shares:

* ``cancelled/<job_id>``: the job was cancelled before it ran
  (``cxxprobe-worker cancel``).
* ``latest/<team>.json``: the newest ``revision_seq`` benchmarked for a team.
  An older job is refused before it runs and refused again before its result
  is published, so a late older result never overwrites a newer one.
* ``rate/<team>.json``: when the team's last benchmark started, for the
  per-team minimum interval.

The markers are best-effort across machines (read-then-write, not a
transaction); the consumer must also keep the highest ``revision_seq`` it has
recorded per team, which the published result carries for that reason.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from cxxprobe_worker.config import BenchConfig
from cxxprobe_worker.jobs import Job
from cxxprobe_worker.monitoring import Logger
from cxxprobe_worker.storage import IArtifactStorage, StorageError

NAMESPACE = "_bench"


def _read_json(storage: IArtifactStorage, name: str) -> dict[str, Any] | None:
    try:
        if not storage.exists(NAMESPACE, name):
            return None
        value = json.loads(storage.get_bytes(NAMESPACE, name))
    except (StorageError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class BenchGuard:
    """Decides whether a benchmark job may run, and whether its result may
    still be published once it has."""

    def __init__(
        self, storage: IArtifactStorage, config: BenchConfig, clock: Any = time.time
    ) -> None:
        self._storage = storage
        self._config = config
        self._clock = clock

    def cancel(self, job_id: str) -> None:
        self._storage.put_text(NAMESPACE, f"cancelled/{job_id}", json.dumps({"at": self._clock()}))

    def latest_seq(self, team_id: str) -> int | None:
        marker = _read_json(self._storage, f"latest/{team_id}.json")
        seq = marker.get("revision_seq") if marker else None
        return seq if isinstance(seq, int) else None

    def refusal(self, job: Job) -> str | None:
        """Why this job must not run, or ``None``. Checked before anything is
        fetched, so a refused job costs the pool nothing."""
        team = job.team_id or ""
        if self._storage.exists(NAMESPACE, f"cancelled/{job.job_id}"):
            return "benchmark cancelled before it ran"
        newest = self.latest_seq(team)
        if newest is not None and job.revision_seq is not None and newest > job.revision_seq:
            return (
                f"superseded: team {team} already has revision_seq {newest}, "
                f"newer than {job.revision_seq}"
            )
        interval = self._config.min_team_interval_seconds
        last = _read_json(self._storage, f"rate/{team}.json")
        started = last.get("started_at") if last else None
        if interval > 0 and isinstance(started, (int, float)):
            waited = self._clock() - started
            if waited < interval:
                return (
                    f"rate limited: team {team} started a benchmark {waited:.0f}s ago; "
                    f"the minimum interval is {interval:.0f}s"
                )
        return None

    def started(self, job: Job) -> None:
        self._storage.put_text(
            NAMESPACE,
            f"rate/{job.team_id}.json",
            json.dumps({"started_at": self._clock(), "job_id": job.job_id}),
        )

    def superseded_after_run(self, job: Job) -> str | None:
        newest = self.latest_seq(job.team_id or "")
        if newest is not None and job.revision_seq is not None and newest > job.revision_seq:
            return f"superseded while running: team {job.team_id} reached revision_seq {newest}"
        return None

    def record(self, job: Job) -> None:
        """Remember this revision as the newest benchmarked for its team."""
        newest = self.latest_seq(job.team_id or "")
        if job.revision_seq is None or (newest is not None and newest >= job.revision_seq):
            return
        self._storage.put_text(
            NAMESPACE,
            f"latest/{job.team_id}.json",
            json.dumps({"revision_seq": job.revision_seq, "job_id": job.job_id}),
        )


@dataclass
class CalibrationState:
    ok: bool = False
    checked_at: float = float("-inf")
    jobs_since: int = 0
    baseline_ns: float | None = None
    last: dict[str, Any] = field(default_factory=dict)
    reason: str = "not calibrated yet"


class Calibrator:
    """Runs ``cxxprobe bench calibrate`` and keeps the machine's health.

    The first calibration sets the baseline when none is configured, so a
    machine at least catches its own drift; a pool should configure one
    shared baseline so that every machine is held to the same speed.
    """

    def __init__(self, binary: str, config: BenchConfig, logger: Logger) -> None:
        self._binary = binary
        self._config = config
        self._log = logger
        self.state = CalibrationState(baseline_ns=config.calibration_baseline_ns)

    def due(self, now: float) -> bool:
        return (
            not self.state.ok
            or now - self.state.checked_at >= self._config.calibration_every_seconds
            or self.state.jobs_since >= self._config.calibration_every_jobs
        )

    def job_done(self) -> None:
        self.state.jobs_since += 1

    def run(self, now: float) -> CalibrationState:
        argv = [
            self._binary,
            "bench",
            "calibrate",
            "--json",
            "--tolerance",
            str(self._config.calibration_tolerance),
            "--noise-threshold",
            str(self._config.noise_threshold),
        ]
        if self._config.cpu >= 0:
            argv += ["--cpu", str(self._config.cpu)]
        if self.state.baseline_ns is not None:
            argv += ["--baseline-ns", f"{self.state.baseline_ns:.0f}"]
        state = self.state
        state.checked_at = now
        state.jobs_since = 0
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
            report = json.loads(done.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            state.ok = False
            state.reason = f"calibration could not run: {exc}"
            self._log.error("bench.calibration_failed", reason=state.reason)
            return state
        state.last = report if isinstance(report, dict) else {}
        state.ok = bool(state.last.get("ok"))
        if state.ok and state.baseline_ns is None:
            state.baseline_ns = float(state.last["median_ns"])
        if state.ok:
            state.reason = ""
        elif state.last.get("noisy"):
            state.reason = "calibration noisy: the machine is contended"
        else:
            state.reason = (
                f"calibration drifted {state.last.get('drift')!r} from baseline "
                f"{state.baseline_ns!r} ns"
            )
        log = self._log.info if state.ok else self._log.error
        log(
            "bench.calibration",
            ok=state.ok,
            median_ns=state.last.get("median_ns"),
            drift=state.last.get("drift"),
            reason=state.reason,
        )
        return state


def summarise_benchmark(report: dict[str, Any], error: str = "") -> dict[str, Any]:
    """What AMS records for a benchmark: the engine's result record with its
    provenance, and the per-part profile. ``verdict`` mirrors the status so a
    consumer that only reads verdicts still sees whether it may rank."""
    record = dict(report.get("result") or {})
    provenance = dict(report.get("provenance") or {})
    profile = report.get("profile") if isinstance(report.get("profile"), dict) else {}
    status = str(record.get("status") or "FAILED_VERIFY")
    return {
        "verdict": "AC" if status == "OK" else ("SE" if status == "NOISY" else "WA"),
        "passed_count": 0,
        "total_count": 0,
        "max_runtime_ms": 0,
        "max_memory_kb": 0,
        "compile_output": str(report.get("verify_reason") or ""),
        "testcases": [],
        "error": error,
        "benchmark": {
            **record,
            **provenance,
            "verify_reason": str(report.get("verify_reason") or ""),
            "attempts": len(report.get("attempts") or []),
            "parts_profile": [
                {
                    "owner": o.get("owner"),
                    "share": o.get("share"),
                    "top_symbols": o.get("top_symbols"),
                }
                for o in (profile or {}).get("owners") or []
                if isinstance(o, dict)
            ],
        },
    }
