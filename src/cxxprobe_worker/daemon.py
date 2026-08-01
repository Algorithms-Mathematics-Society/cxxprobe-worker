"""The poll loop.

Claim a job, run it, complete or release it, repeat. Everything interesting
lives in the collaborators — this file is only responsible for *when* work
happens and for stopping cleanly.

Graceful shutdown means: stop claiming new jobs, let in-flight ones finish,
then exit. Killing a job mid-judge would leave the queue's lease to expire
and the job to be redone anyway, so there is nothing to gain by being
abrupt.
"""

from __future__ import annotations

import signal
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import FrameType

from cxxprobe_worker import __version__
from cxxprobe_worker.config import WorkerConfig
from cxxprobe_worker.control_plane import ControlPlaneClient
from cxxprobe_worker.executor import JobExecutor
from cxxprobe_worker.jobs import JobResult
from cxxprobe_worker.monitoring import HealthReporter, Logger, Metrics
from cxxprobe_worker.queue import IJobQueue, Lease, QueueError


class Worker:
    """Owns the poll loop and the pool of job threads."""

    def __init__(
        self,
        config: WorkerConfig,
        queue: IJobQueue,
        executor: JobExecutor,
        logger: Logger,
        metrics: Metrics,
        health: HealthReporter,
        control_plane: ControlPlaneClient | None = None,
    ) -> None:
        self._config = config
        self._queue = queue
        self._executor = executor
        self._log = logger
        self._metrics = metrics
        self._health = health
        self._control_plane = control_plane
        self._stopping = threading.Event()
        self._jobs_claimed = 0

    @property
    def metrics(self) -> Metrics:
        return self._metrics

    def request_stop(self) -> None:
        """Ask the loop to finish its in-flight work and return."""
        if not self._stopping.is_set():
            self._log.info("worker.stopping")
        self._stopping.set()

    def install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: FrameType | None) -> None:
            self._log.info("worker.signal", signal=signal.Signals(signum).name)
            self.request_stop()

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _should_claim_more(self) -> bool:
        if self._stopping.is_set():
            return False
        cap = self._config.max_jobs
        return cap is None or self._jobs_claimed < cap

    def _handle_result(self, lease: Lease, result: JobResult) -> None:
        """Complete or release a job based on how it ended.

        Only RETRYABLE goes back on the queue. A FAILED job is permanently
        broken and would fail identically forever; a SUCCEEDED job is done
        regardless of what verdict the submission earned.
        """
        try:
            if result.should_retry:
                self._queue.release(lease)
                self._metrics.incr("jobs_retried")
            else:
                self._queue.complete(lease)
                self._metrics.incr("jobs_succeeded" if result.ok else "jobs_failed")
        except QueueError as exc:
            # The job ran; we just can't record that. The lease will expire
            # and it will be redone — at-least-once is the contract.
            self._log.error("queue.finalize_failed", job_id=result.job_id, error=str(exc))

    def _run_one(self, lease: Lease) -> None:
        try:
            result = self._executor.execute(lease.job)
        except Exception as exc:  # a job must never take the worker down
            self._log.error("job.crashed", job_id=lease.job.job_id, error=str(exc))
            self._queue.release(lease)
            self._metrics.incr("jobs_crashed")
            return
        self._handle_result(lease, result)

    def run(self) -> int:
        """Poll until stopped or ``max_jobs`` is reached. Returns jobs processed."""
        if self._control_plane is not None and self._control_plane.enabled:
            self._control_plane.register(hostname=self._config.worker_id, version=__version__)

        self._log.info(
            "worker.start",
            worker_id=self._config.worker_id,
            environment=self._config.environment,
            concurrency=self._config.concurrency,
            max_jobs=self._config.max_jobs,
        )
        self._health.write("running")

        processed = 0
        pending: set[Future[None]] = set()
        with ThreadPoolExecutor(max_workers=self._config.concurrency) as pool:
            while self._should_claim_more():
                pending = {f for f in pending if not f.done()}
                if len(pending) >= self._config.concurrency:
                    time.sleep(min(self._config.queue.poll_interval_seconds, 0.05))
                    continue

                try:
                    lease = self._queue.claim()
                except QueueError as exc:
                    # The queue itself is unreachable. Back off rather than
                    # spinning — this is usually a transient mount problem.
                    self._log.error("queue.claim_failed", error=str(exc))
                    self._sleep_between_polls()
                    continue

                if lease is None:
                    self._health.write("idle")
                    self._sleep_between_polls()
                    continue

                self._jobs_claimed += 1
                processed += 1
                self._metrics.incr("jobs_claimed")
                pending.add(pool.submit(self._run_one, lease))
                self._health.write("running")

            # Exiting the `with` joins the pool, which is exactly the
            # "let in-flight jobs finish" half of graceful shutdown.
            if pending:
                self._log.info("worker.draining", in_flight=len(pending))

        self._health.write("stopped")
        self._log.info("worker.stop", jobs_processed=processed, **self._metrics.snapshot())
        return processed

    def run_once(self) -> JobResult | None:
        """Claim and run at most one job, then return. Used by ``--once``."""
        if self._control_plane is not None and self._control_plane.enabled:
            self._control_plane.register(hostname=self._config.worker_id, version=__version__)
        try:
            lease = self._queue.claim()
        except QueueError as exc:
            self._log.error("queue.claim_failed", error=str(exc))
            return None
        if lease is None:
            return None

        self._metrics.incr("jobs_claimed")
        result = self._executor.execute(lease.job)
        self._handle_result(lease, result)
        return result

    def _sleep_between_polls(self) -> None:
        # Waiting on the stop event rather than sleeping flat means SIGTERM
        # is honoured immediately instead of after the full poll interval.
        self._stopping.wait(self._config.queue.poll_interval_seconds)
