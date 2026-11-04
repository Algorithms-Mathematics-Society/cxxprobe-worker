"""Benchmark jobs (#12, #13): one job at a time, on a calibrated machine, for
the newest revision, on verified code, with the provenance that lets a number
be traced.

The engine reports in tests/fixtures/bench are `cxxprobe bench --json` output
captured from the real engine (cxxprobe with #10/#11), scratch paths
rewritten. A stand-in binary prints the one each test needs, with the exit
code the engine gave it, and writes the profile files when asked to.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from cxxprobe_worker.bench import BenchGuard, Calibrator
from cxxprobe_worker.config import BenchConfig, JudgeConfig, WorkerConfig
from cxxprobe_worker.control_plane import summarise
from cxxprobe_worker.daemon import Worker
from cxxprobe_worker.executor import JobExecutor
from cxxprobe_worker.jobs import Job, JobError, JobResult, JobStatus
from cxxprobe_worker.monitoring import HealthReporter, Metrics

FIXTURES = Path(__file__).parent / "fixtures" / "bench"
EXIT = {"ok": 0, "noisy": 1, "failed_verify": 1, "calibrate_ok": 0, "calibrate_drift": 1}
PROJECT_YAML = (
    "version: 1\nname: The Order Desk\nparts:\n"
    '  - {dir: parts/1-ingest, label: "1"}\n'
    '  - {dir: parts/2-book, label: "2"}\n'
    '  - {dir: parts/3-ship, label: "3"}\n'
)


def fake_engine(tmp_path: Path, name: str, before: str = "") -> tuple[Path, Path]:
    """Answers `bench project`/`bench calibrate` with a captured report and
    writes flamegraph.svg/stacks.folded into the --profile directory."""
    calls = tmp_path / "calls.log"
    script = tmp_path / f"fake-cxxprobe-{name}"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'if [ "$1" = "package" ]; then exit 0; fi\n'
        f"{before}\n"
        'prev=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "--profile" ]; then mkdir -p "$arg"; '
        'echo "<svg/>" > "$arg/flamegraph.svg"; echo "main;ingest 9" > "$arg/stacks.folded"; fi\n'
        '  prev="$arg"\n'
        "done\n"
        f'cat "{FIXTURES / name}.json"\n'
        f"exit {EXIT[name]}\n"
    )
    script.chmod(0o755)
    return script, calls


def make_contest(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    contest = tmp_path / "round-2"
    (contest / "order-desk").mkdir(parents=True)
    (contest / "contest.yaml").write_text("version: 1\nname: Round 2\n")
    (contest / "order-desk" / "project.yaml").write_text(PROJECT_YAML)
    parts = {}
    for label in ("1", "2", "3"):
        src = tmp_path / f"part-{label}.cpp"
        src.write_text(f"// part {label}\n")
        parts[label] = str(src)
    hidden = tmp_path / "hidden-input.txt"
    hidden.write_text("400000\n")
    return contest, parts, hidden


def bench_job(package: Path, parts: dict[str, str], hidden: Path, **extra: Any) -> Job:
    fields: dict[str, Any] = {
        "job_id": "b1",
        "job_type": "benchmark",
        "project_slug": "order-desk",
        "package_path": str(package),
        "parts": parts,
        "team_id": "team-7",
        "revision": "9f1c2ab",
        "revision_seq": 3,
        "benchmark_input": str(hidden),
    }
    fields.update(extra)
    return Job(**fields)


class AlwaysCalibrated:
    """A calibrator stand-in whose machine is healthy."""

    def __init__(self, ok: bool = True) -> None:
        from cxxprobe_worker.bench import CalibrationState

        self.state = CalibrationState(ok=ok, reason="" if ok else "calibration drifted")
        self.jobs = 0

    def job_done(self) -> None:
        self.jobs += 1


def executor_for(
    binary: Path,
    workspaces: Any,
    storage: Any,
    logger: Any,
    *,
    calibrated: bool = True,
    guard: bool = True,
    **bench: Any,
) -> JobExecutor:
    config = BenchConfig(**bench)
    return JobExecutor(
        JudgeConfig(binary=str(binary)),
        workspaces,
        storage,
        logger,
        bench=config,
        bench_guard=BenchGuard(storage, config) if guard else None,
        calibrator=AlwaysCalibrated(calibrated),  # type: ignore[arg-type]
        machine_id="bench-ap-south-1a-01",
    )


# ── the job and the role ──────────────────────────────────────────────


def test_a_benchmark_job_needs_its_team_revision_and_hidden_input(tmp_path):
    contest, parts, hidden = make_contest(tmp_path)
    for missing in ("team_id", "revision", "revision_seq", "benchmark_input"):
        with pytest.raises(ValueError, match=missing):
            bench_job(contest, parts, hidden, **{missing: None})
    with pytest.raises(ValueError, match="no fallback"):
        bench_job(contest, parts, hidden, fallback={"1": "reference"})
    with pytest.raises(ValueError, match="only a benchmark job"):
        Job(job_id="j", package_path="/p", submission_path="/s", team_id="t")
    with pytest.raises(JobError):
        Job.from_dict({"job_id": "j", "job_type": "benchmark", "package_path": "/p"})


def test_a_bencher_never_runs_two_jobs_at_once():
    with pytest.raises(ValueError, match="concurrency must be 1"):
        WorkerConfig(role="bencher", concurrency=2)
    assert WorkerConfig(role="bencher").concurrency == 1


# ── running one (#12, #13) ────────────────────────────────────────────


def test_an_ok_benchmark_is_timed_once_and_carries_its_provenance(
    tmp_path, workspaces, storage, logger
):
    contest, parts, hidden = make_contest(tmp_path)
    binary, calls = fake_engine(tmp_path, "ok")

    result = executor_for(binary, workspaces, storage, logger).execute(
        bench_job(contest, parts, hidden)
    )

    assert result.status is JobStatus.SUCCEEDED, result.error
    (argv,) = calls.read_text().splitlines()
    assert argv.startswith("bench project order-desk --dir ")
    for label in ("1", "2", "3"):
        assert f"--submission {label}=" in argv
    assert " --input " in argv and "benchmark-input" in argv
    assert "--repetitions 7 --warmups 1 --attempts 3 --noise-threshold 0.05" in argv
    assert " --profile " in argv and argv.endswith(" --json")

    assert result.provenance == {
        "machine_id": "bench-ap-south-1a-01",
        "calibration_ok": True,
        "input_hash": hashlib.sha256(b"400000\n").hexdigest(),
        "revision_hash": "9f1c2ab",
        "team_id": "team-7",
        "revision_seq": 3,
    }
    for name in ("report.json", "profile/flamegraph.svg", "profile/stacks.folded"):
        assert name in result.artifacts
    stored = json.loads(Path(result.artifacts["report.json"]).read_text())
    assert stored["provenance"]["input_hash"] == result.provenance["input_hash"]

    out = summarise(result.report)
    assert out["verdict"] == "AC"
    record = out["benchmark"]
    assert record["status"] == "OK"
    assert record["median_ns"] > 0 and record["repetitions"] == 7
    assert record["machine_id"] == "bench-ap-south-1a-01"
    assert record["input_hash"] == result.provenance["input_hash"]
    assert record["revision_hash"] == "9f1c2ab" and record["revision_seq"] == 3
    assert record["parts_profile"][0]["owner"] == "1"  # the hot part, first by share


def test_a_noisy_benchmark_is_retried_not_scored(tmp_path, workspaces, storage, logger):
    contest, parts, hidden = make_contest(tmp_path)
    binary, _ = fake_engine(tmp_path, "noisy")

    result = executor_for(binary, workspaces, storage, logger).execute(
        bench_job(contest, parts, hidden)
    )

    assert result.status is JobStatus.RETRYABLE
    assert "noisy" in (result.error or "")
    assert BenchGuard(storage, BenchConfig()).latest_seq("team-7") is None


def test_code_that_fails_on_the_benchmark_input_is_rejected_with_its_reason(
    tmp_path, workspaces, storage, logger
):
    contest, parts, hidden = make_contest(tmp_path)
    binary, _ = fake_engine(tmp_path, "failed_verify")

    result = executor_for(binary, workspaces, storage, logger).execute(
        bench_job(contest, parts, hidden)
    )

    assert result.status is JobStatus.FAILED
    assert "rejected from benchmarking: output differs from the reference" in (result.error or "")
    assert summarise(result.report)["benchmark"]["status"] == "FAILED_VERIFY"
    assert summarise(result.report)["benchmark"]["median_ns"] is None


def test_every_part_must_be_supplied(tmp_path, workspaces, storage, logger):
    contest, parts, hidden = make_contest(tmp_path)
    del parts["2"]
    binary, calls = fake_engine(tmp_path, "ok")

    result = executor_for(binary, workspaces, storage, logger).execute(
        bench_job(contest, parts, hidden)
    )

    assert result.status is JobStatus.FAILED
    assert "has no submission for part 2" in (result.error or "")
    assert not calls.exists()


def test_an_unhealthy_bencher_hands_the_job_back(tmp_path, workspaces, storage, logger):
    contest, parts, hidden = make_contest(tmp_path)
    binary, calls = fake_engine(tmp_path, "ok")

    result = executor_for(binary, workspaces, storage, logger, calibrated=False).execute(
        bench_job(contest, parts, hidden)
    )

    assert result.status is JobStatus.RETRYABLE
    assert "bencher unhealthy" in (result.error or "")
    assert not calls.exists()


# ── supersession, cancellation, rate limits ───────────────────────────


def test_an_older_revision_never_runs_once_a_newer_one_is_recorded(
    tmp_path, workspaces, storage, logger
):
    contest, parts, hidden = make_contest(tmp_path)
    binary, calls = fake_engine(tmp_path, "ok")
    executor = executor_for(binary, workspaces, storage, logger, min_team_interval_seconds=0)

    newer = executor.execute(bench_job(contest, parts, hidden, job_id="new", revision_seq=5))
    assert newer.status is JobStatus.SUCCEEDED
    older = executor.execute(bench_job(contest, parts, hidden, job_id="old", revision_seq=4))

    assert older.status is JobStatus.FAILED
    assert "superseded" in (older.error or "")
    assert len(calls.read_text().splitlines()) == 1


def test_a_late_older_result_does_not_overwrite_a_newer_one(tmp_path, workspaces, storage, logger):
    contest, parts, hidden = make_contest(tmp_path)
    # While this job runs, a newer revision of the same team finishes elsewhere.
    marker = storage.root / "_bench" / "latest" / "team-7.json"
    before = (
        f'mkdir -p "{marker.parent}"; '
        f'echo \'{{"revision_seq": 9, "job_id": "elsewhere"}}\' > "{marker}"'
    )
    binary, _ = fake_engine(tmp_path, "ok", before=before)

    result = executor_for(binary, workspaces, storage, logger).execute(
        bench_job(contest, parts, hidden, revision_seq=3)
    )

    assert result.status is JobStatus.FAILED
    assert "superseded while running" in (result.error or "")
    assert BenchGuard(storage, BenchConfig()).latest_seq("team-7") == 9


def test_a_cancelled_job_is_refused(tmp_path, workspaces, storage, logger):
    contest, parts, hidden = make_contest(tmp_path)
    binary, calls = fake_engine(tmp_path, "ok")
    BenchGuard(storage, BenchConfig()).cancel("b1")

    result = executor_for(binary, workspaces, storage, logger).execute(
        bench_job(contest, parts, hidden)
    )

    assert result.status is JobStatus.FAILED
    assert "cancelled" in (result.error or "")
    assert not calls.exists()


def test_a_team_is_rate_limited(tmp_path, workspaces, storage, logger):
    contest, parts, hidden = make_contest(tmp_path)
    binary, calls = fake_engine(tmp_path, "ok")
    executor = executor_for(binary, workspaces, storage, logger, min_team_interval_seconds=300)

    first = executor.execute(bench_job(contest, parts, hidden, job_id="a", revision_seq=1))
    second = executor.execute(bench_job(contest, parts, hidden, job_id="b", revision_seq=2))
    other_team = executor.execute(
        bench_job(contest, parts, hidden, job_id="c", revision_seq=1, team_id="team-8")
    )

    assert first.status is JobStatus.SUCCEEDED
    assert second.status is JobStatus.FAILED
    assert "rate limited: team team-7" in (second.error or "")
    assert other_team.status is JobStatus.SUCCEEDED
    assert len(calls.read_text().splitlines()) == 2


# ── calibration and the worker loop ───────────────────────────────────


def test_calibration_sets_a_baseline_then_catches_drift(tmp_path, logger):
    ok, calls = fake_engine(tmp_path, "calibrate_ok")
    calibrator = Calibrator(str(ok), BenchConfig(), logger)

    assert calibrator.due(0.0)
    assert calibrator.run(0.0).ok
    assert calibrator.state.baseline_ns == pytest.approx(
        json.loads((FIXTURES / "calibrate_ok.json").read_text())["median_ns"]
    )
    assert "--baseline-ns" not in calls.read_text()
    assert not calibrator.due(1.0)

    drifted, calls = fake_engine(tmp_path, "calibrate_drift")
    calibrator._binary = str(drifted)
    state = calibrator.run(2000.0)
    assert not state.ok
    assert "drifted" in state.reason
    assert "--baseline-ns" in calls.read_text().splitlines()[-1]
    assert calibrator.due(2001.0)


def test_calibration_is_due_again_after_enough_jobs(tmp_path, logger):
    ok, _ = fake_engine(tmp_path, "calibrate_ok")
    calibrator = Calibrator(str(ok), BenchConfig(calibration_every_jobs=2), logger)
    calibrator.run(0.0)
    calibrator.job_done()
    assert not calibrator.due(1.0)
    calibrator.job_done()
    assert calibrator.due(1.0)


class RecordingExecutor:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def execute(self, job: Job) -> JobResult:
        self.seen.append(job.job_id)
        return JobResult(job_id=job.job_id, status=JobStatus.SUCCEEDED)


def worker_for(
    config: WorkerConfig,
    job_queue: Any,
    logger: Any,
    executor: Any,
    calibrator: Calibrator | None = None,
    health_file: Path | None = None,
) -> Worker:
    metrics = Metrics()
    return Worker(
        config,
        job_queue,
        executor,
        logger,
        metrics,
        HealthReporter(health_file, config.worker_id, metrics),
        calibrator=calibrator,
    )


def test_an_unhealthy_bencher_stops_claiming_and_says_why(config, job_queue, logger, tmp_path):
    drifted, _ = fake_engine(tmp_path, "calibrate_drift")
    bencher = config.model_copy(update={"role": "bencher"})
    calibrator = Calibrator(str(drifted), BenchConfig(calibration_baseline_ns=1000), logger)
    contest, parts, hidden = make_contest(tmp_path)
    job_queue.publish(bench_job(contest, parts, hidden))
    executor = RecordingExecutor()
    health = tmp_path / "health.json"

    assert worker_for(bencher, job_queue, logger, executor, calibrator, health).run_once() is None

    assert executor.seen == []
    assert json.loads(health.read_text())["state"].startswith("unhealthy: calibration drifted")
    assert job_queue.claim() is not None  # still queued for a healthy bencher


def test_a_job_for_the_other_role_goes_back_untouched(config, job_queue, logger, tmp_path):
    contest, parts, hidden = make_contest(tmp_path)
    job_queue.publish(bench_job(contest, parts, hidden))
    executor = RecordingExecutor()

    assert worker_for(config, job_queue, logger, executor).run_once() is None
    assert executor.seen == []

    bencher = config.model_copy(update={"role": "bencher"})
    ok, _ = fake_engine(tmp_path, "calibrate_ok")
    calibrator = Calibrator(str(ok), BenchConfig(), logger)
    result = worker_for(bencher, job_queue, logger, executor, calibrator).run_once()
    assert result is not None and result.ok
    assert executor.seen == ["b1"]


# ── against the real engine, when one is available ────────────────────


@pytest.mark.skipif(
    not (os.environ.get("CXXPROBE_BINARY") or shutil.which("cxxprobe")),
    reason="no cxxprobe binary (set CXXPROBE_BINARY)",
)
def test_a_real_benchmark_job_end_to_end(tmp_path, workspaces, storage, logger):
    binary = os.environ.get("CXXPROBE_BINARY") or shutil.which("cxxprobe") or ""
    contest, parts, hidden = make_contest(tmp_path)
    desk = contest / "order-desk"
    (desk / "shared").mkdir()
    (desk / "shared" / "desk.hpp").write_text(
        "#pragma once\nint ingest(int);\nint book(int);\nint ship(int);\n"
    )
    bodies = {
        "1-ingest": "int ingest(int x) { volatile int a = 0; "
        "for (int i = 0; i < 200; ++i) a = a + i % 7; return x * 2 + (a - a); }",
        "2-book": "int book(int x) { return x + 5; }",
        "3-ship": "int ship(int x) { return x - 1; }",
    }
    for (directory, body), label in zip(bodies.items(), ("1", "2", "3"), strict=True):
        part = desk / "parts" / directory
        (part / "solutions").mkdir(parents=True)
        (part / "problem.yaml").write_text(f"version: 2\nname: {directory}\n")
        source = f'#include "desk.hpp"\n{body}\nint main() {{ return 0; }}\n'
        (part / "solutions" / "main.cpp").write_text(source)
        Path(parts[label]).write_text(source)
    (desk / "benchmark").mkdir()
    (desk / "benchmark" / "bench_main.cpp").write_text(
        '#include <iostream>\n#include "desk.hpp"\nint main() { long n = 0; std::cin >> n; '
        "long t = 0; for (long i = 0; i < n; ++i) t += ship(book(ingest(int(i % 1000)))); "
        "std::cout << t << '\\n'; }\n"
    )
    (desk / "benchmark" / "input.txt").write_text("1000\n")

    executor = JobExecutor(
        JudgeConfig(binary=binary),
        workspaces,
        storage,
        logger,
        bench=BenchConfig(noise_threshold=0.25, attempts=5),
        bench_guard=BenchGuard(storage, BenchConfig()),
        calibrator=AlwaysCalibrated(),  # type: ignore[arg-type]
        machine_id="local",
    )
    result = executor.execute(bench_job(contest, parts, hidden))
    if result.status is JobStatus.RETRYABLE and "could not run" in (result.error or ""):
        pytest.skip(f"engine cannot benchmark here: {result.error}")
    assert result.status is JobStatus.SUCCEEDED, result.error
    record = summarise(result.report)["benchmark"]
    assert record["status"] == "OK"
    assert record["input_hash"] == hashlib.sha256(b"400000\n").hexdigest()
    assert any(p["owner"] == "1" and p["share"] > 0.5 for p in record["parts_profile"])
