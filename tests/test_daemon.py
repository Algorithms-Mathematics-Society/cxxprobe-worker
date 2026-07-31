from __future__ import annotations

import threading
import time
from pathlib import Path

from cxxprobe_worker.app import build_application
from cxxprobe_worker.config import WorkerConfig
from cxxprobe_worker.daemon import Worker
from cxxprobe_worker.jobs import Job, JobResult, JobStatus
from cxxprobe_worker.monitoring import HealthReporter, Metrics
from cxxprobe_worker.queue import LocalJobQueue


class StubExecutor:
    """Records what it was asked to run and returns a scripted outcome."""

    def __init__(self, status: JobStatus = JobStatus.SUCCEEDED, delay: float = 0.0) -> None:
        self.status = status
        self.delay = delay
        self.seen: list[str] = []
        self._lock = threading.Lock()

    def execute(self, job: Job) -> JobResult:
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.seen.append(job.job_id)
        return JobResult(job_id=job.job_id, status=self.status)


class CrashingExecutor:
    def execute(self, job: Job) -> JobResult:
        raise RuntimeError("executor exploded")


def make_worker(config: WorkerConfig, queue: LocalJobQueue, executor, logger) -> Worker:
    metrics = Metrics()
    return Worker(config, queue, executor, logger, metrics, HealthReporter(None, "w", metrics))


def publish(queue: LocalJobQueue, tmp_path: Path, count: int) -> None:
    for i in range(count):
        queue.publish(
            Job(
                job_id=f"job-{i}",
                package_path=tmp_path / "pkg",
                submission_path=tmp_path / "sub.cpp",
            )
        )


def test_run_once_on_empty_queue_returns_none(config, job_queue, logger):
    worker = make_worker(config, job_queue, StubExecutor(), logger)
    assert worker.run_once() is None


def test_run_once_processes_and_completes_a_job(config, job_queue, logger, tmp_path):
    publish(job_queue, tmp_path, 1)
    executor = StubExecutor()
    worker = make_worker(config, job_queue, executor, logger)

    result = worker.run_once()
    assert result is not None
    assert result.ok
    assert executor.seen == ["job-0"]
    assert job_queue.depth() == 0
    assert job_queue.inflight_count() == 0


def test_retryable_result_returns_the_job_to_the_queue(config, job_queue, logger, tmp_path):
    publish(job_queue, tmp_path, 1)
    worker = make_worker(config, job_queue, StubExecutor(JobStatus.RETRYABLE), logger)

    worker.run_once()
    assert job_queue.depth() == 1


def test_failed_result_does_not_return_the_job_to_the_queue(config, job_queue, logger, tmp_path):
    # A permanently-broken job must leave the queue, or it poisons the worker
    # forever.
    publish(job_queue, tmp_path, 1)
    worker = make_worker(config, job_queue, StubExecutor(JobStatus.FAILED), logger)

    worker.run_once()
    assert job_queue.depth() == 0
    assert job_queue.inflight_count() == 0


def test_run_stops_after_max_jobs(config, job_queue, logger, tmp_path):
    publish(job_queue, tmp_path, 5)
    config = config.model_copy(update={"max_jobs": 3})
    executor = StubExecutor()
    worker = make_worker(config, job_queue, executor, logger)

    assert worker.run() == 3
    assert len(executor.seen) == 3
    assert job_queue.depth() == 2


def test_run_drains_the_queue_then_keeps_polling_until_stopped(config, job_queue, logger, tmp_path):
    publish(job_queue, tmp_path, 2)
    executor = StubExecutor()
    worker = make_worker(config, job_queue, executor, logger)

    thread = threading.Thread(target=worker.run)
    thread.start()
    deadline = time.monotonic() + 5
    while len(executor.seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.request_stop()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert sorted(executor.seen) == ["job-0", "job-1"]


def test_request_stop_before_run_returns_immediately(config, job_queue, logger, tmp_path):
    publish(job_queue, tmp_path, 3)
    worker = make_worker(config, job_queue, StubExecutor(), logger)
    worker.request_stop()
    assert worker.run() == 0
    assert job_queue.depth() == 3


def test_a_crashing_executor_releases_the_job_and_keeps_the_worker_alive(
    config, job_queue, logger, tmp_path
):
    publish(job_queue, tmp_path, 1)
    worker = make_worker(config, job_queue, CrashingExecutor(), logger)
    config = config.model_copy(update={"max_jobs": 1})

    worker._config = config
    assert worker.run() == 1
    # Released, not lost: an executor bug shouldn't silently drop work.
    assert job_queue.depth() == 1
    assert worker.metrics.get("jobs_crashed") == 1


def test_concurrency_runs_jobs_in_parallel(config, job_queue, logger, tmp_path):
    publish(job_queue, tmp_path, 4)
    config = config.model_copy(update={"concurrency": 4, "max_jobs": 4})
    executor = StubExecutor(delay=0.15)
    worker = make_worker(config, job_queue, executor, logger)

    started = time.monotonic()
    worker.run()
    elapsed = time.monotonic() - started

    assert len(executor.seen) == 4
    # Serially this is 0.6s; in parallel it should be well under that.
    assert elapsed < 0.45, f"took {elapsed:.2f}s — jobs did not overlap"


def test_metrics_count_outcomes(config, job_queue, logger, tmp_path):
    publish(job_queue, tmp_path, 2)
    config = config.model_copy(update={"max_jobs": 2})
    worker = make_worker(config, job_queue, StubExecutor(), logger)
    worker.run()

    assert worker.metrics.get("jobs_claimed") == 2
    assert worker.metrics.get("jobs_succeeded") == 2


def test_build_application_wires_everything_together(config):
    app = build_application(config)
    assert app.worker is not None
    assert app.queue is not None
    assert app.storage is not None
    assert app.executor is not None
