"""Where jobs come from.

``IJobQueue`` is the seam an SQS backend would implement. Only the local
filesystem spool exists today — it is what makes the worker runnable and
testable without any cloud dependency, and it is a real queue rather than a
stub: claims are atomic, visibility timeouts expire, and a crashed worker's
job becomes available again.

The lease model matches SQS deliberately, so swapping the backend doesn't
change the daemon:

    claim()      → hand out a job and hide it for visibility_timeout seconds
    complete()   → the job is done; delete it
    release()    → put it back now, don't wait for the timeout
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from cxxprobe_worker.jobs import Job, JobError


class QueueError(RuntimeError):
    """Raised when the queue's own storage is unusable."""


@dataclass(frozen=True)
class Lease:
    """A claimed job plus the token needed to complete or release it."""

    job: Job
    receipt: str
    """Opaque handle. For the local queue it's the in-flight file's name."""

    delivery_attempt: int = 1
    """How many times this job has been handed out, including now."""


@runtime_checkable
class IJobQueue(Protocol):
    def claim(self) -> Lease | None:
        """Take one job, or return None if the queue is empty."""
        ...

    def complete(self, lease: Lease) -> None:
        """Permanently remove a finished job."""
        ...

    def release(self, lease: Lease) -> None:
        """Return a job for redelivery."""
        ...

    def publish(self, job: Job) -> None:
        """Enqueue a job. Mainly for tests and the `submit` CLI verb."""
        ...

    def depth(self) -> int:
        """Jobs waiting to be claimed. Excludes in-flight ones."""
        ...


class LocalJobQueue:
    """A filesystem spool: ``pending/`` and ``inflight/`` directories of JSON.

    Claiming is a ``Path.rename`` (``os.rename``) from pending to inflight,
    which is atomic on
    POSIX — two workers racing for the same job means exactly one rename
    succeeds and the other gets ENOENT and moves on. That's the whole
    concurrency story; no locking required.
    """

    def __init__(
        self,
        root: Path,
        visibility_timeout_seconds: float = 600.0,
        max_delivery_attempts: int = 5,
    ) -> None:
        self._root = Path(root)
        self._pending = self._root / "pending"
        self._inflight = self._root / "inflight"
        self._visibility = visibility_timeout_seconds
        self._max_attempts = max_delivery_attempts

    @property
    def root(self) -> Path:
        return self._root

    def _ensure_dirs(self) -> None:
        try:
            self._pending.mkdir(parents=True, exist_ok=True)
            self._inflight.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise QueueError(f"cannot create queue directories under {self._root}: {exc}") from exc

    def publish(self, job: Job) -> None:
        self._ensure_dirs()
        # uuid suffix so republishing the same job_id (a retry) doesn't
        # overwrite the copy already waiting.
        name = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}-{job.job_id}.json"
        path = self._pending / name
        try:
            path.write_text(job.model_dump_json(indent=2))
        except OSError as exc:
            raise QueueError(f"cannot publish job {job.job_id}: {exc}") from exc

    def claim(self) -> Lease | None:
        self._ensure_dirs()
        self._reclaim_expired()

        for candidate in sorted(self._pending.glob("*.json")):
            target = self._inflight / candidate.name
            try:
                candidate.rename(target)
            except OSError:
                # Lost the race to another worker, or the file vanished.
                continue

            try:
                payload = json.loads(target.read_text())
                job = Job.from_dict(payload)
            except (OSError, json.JSONDecodeError, JobError):
                # Unparseable jobs must leave the queue, or every poll
                # rediscovers them forever. Park them for inspection.
                self._quarantine(target)
                continue

            attempt = _attempts_of(target.name) + 1
            if attempt > self._max_attempts:
                # A job classified retryable can still be permanently broken
                # (a corrupt package looks the same as a bad host on the
                # first attempt). Without this cap it would recirculate for
                # ever, and would eventually be the only thing the worker
                # ever does.
                self._quarantine(target)
                continue

            return Lease(job=job, receipt=target.name, delivery_attempt=attempt)
        return None

    def complete(self, lease: Lease) -> None:
        path = self._inflight / lease.receipt
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise QueueError(f"cannot complete job {lease.job.job_id}: {exc}") from exc

    def release(self, lease: Lease) -> None:
        source = self._inflight / lease.receipt
        if not source.exists():
            return
        try:
            source.rename(self._pending / _with_attempts(lease.receipt, lease.delivery_attempt))
        except OSError as exc:
            raise QueueError(f"cannot release job {lease.job.job_id}: {exc}") from exc

    def depth(self) -> int:
        if not self._pending.is_dir():
            return 0
        return len(list(self._pending.glob("*.json")))

    def inflight_count(self) -> int:
        if not self._inflight.is_dir():
            return 0
        return len(list(self._inflight.glob("*.json")))

    def _reclaim_expired(self) -> None:
        """Return in-flight jobs whose lease has lapsed.

        This is what makes a killed worker recoverable: nothing else knows
        its job existed, so the timeout is the only path back.
        """
        if not self._inflight.is_dir():
            return
        cutoff = time.time() - self._visibility
        for path in self._inflight.glob("*.json"):
            try:
                if path.stat().st_mtime > cutoff:
                    continue
                path.rename(self._pending / path.name)
            except OSError:
                continue

    def dead_letter_count(self) -> int:
        dead = self._root / "dead"
        if not dead.is_dir():
            return 0
        return len(list(dead.glob("*.json")))

    def _quarantine(self, path: Path) -> None:
        dead = self._root / "dead"
        try:
            dead.mkdir(parents=True, exist_ok=True)
            path.rename(dead / path.name)
        except OSError:
            path.unlink(missing_ok=True)


# Delivery count is carried in the filename (``...#3.json``) rather than in
# the payload, so a redelivery never has to rewrite the job itself — the
# rename that moves it between directories is the only write.
ATTEMPT_MARKER = "#"


def _attempts_of(name: str) -> int:
    stem = name.removesuffix(".json")
    _, marker, count = stem.rpartition(ATTEMPT_MARKER)
    if not marker or not count.isdigit():
        return 0
    return int(count)


def _with_attempts(name: str, attempts: int) -> str:
    stem = name.removesuffix(".json")
    base, marker, count = stem.rpartition(ATTEMPT_MARKER)
    if marker and count.isdigit():
        stem = base
    return f"{stem}{ATTEMPT_MARKER}{attempts}.json"


def build_queue(
    backend: str,
    root: Path,
    visibility_timeout_seconds: float,
    max_delivery_attempts: int = 5,
) -> IJobQueue:
    if backend == "local":
        return LocalJobQueue(root, visibility_timeout_seconds, max_delivery_attempts)
    raise ValueError(f"unknown queue backend: {backend!r}")
