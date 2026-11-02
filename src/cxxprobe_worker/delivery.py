"""Durable, bounded callback delivery using the existing artifact and job backends."""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from cxxprobe_worker.config import WorkerConfig
from cxxprobe_worker.control_plane import IControlPlane
from cxxprobe_worker.jobs import Job, JobResult
from cxxprobe_worker.monitoring import Logger
from cxxprobe_worker.queue import IJobQueue, Lease
from cxxprobe_worker.storage import IArtifactStorage, StorageError

LOOKUP_LIMIT = 32


def attempt_prefix(job: Job) -> str:
    if job.attempt_token is None:
        raise StorageError("durable delivery requires metadata.attempt_token")
    digest = hashlib.sha256(job.attempt_token.encode()).hexdigest()
    return f"results/{digest}/"


class DeliveryState(BaseModel):
    model_config = {"extra": "forbid", "allow_inf_nan": False}

    version: Literal[1] = 1
    artifact: str
    round: int = Field(ge=0, le=1000)
    deadline: float
    max_rounds: int = Field(ge=1, le=100)
    exhausted: bool = False


class DeliveryEnvelope(BaseModel):
    model_config = {"extra": "forbid", "allow_inf_nan": False}

    version: Literal[1] = 1
    job: Job
    result: JobResult
    payload: dict[str, Any]
    target_tier: str
    source_attempt: int = Field(ge=1)
    created_at: float
    deadline: float
    max_rounds: int = Field(ge=1, le=100)


class ResultDelivery:
    def __init__(
        self,
        config: WorkerConfig,
        storage: IArtifactStorage,
        queue: IJobQueue,
        plane: IControlPlane,
        logger: Logger,
    ) -> None:
        self._config = config
        self._storage = storage
        self._queue = queue
        self._plane = plane
        self._log = logger

    def load(self, job: Job, artifact: str) -> DeliveryEnvelope:
        prefix = attempt_prefix(job)
        suffix = artifact.removeprefix(prefix)
        if not artifact.startswith(prefix) or "/" in suffix or not suffix.endswith(".json"):
            raise StorageError("delivery artifact is outside this job attempt")
        try:
            envelope = DeliveryEnvelope.model_validate_json(
                self._storage.get_bytes(job.job_id, artifact)
            )
        except ValidationError as exc:
            raise StorageError(f"invalid delivery envelope: {exc}") from exc
        if (
            envelope.job.job_id != job.job_id
            or envelope.job.attempt_token != job.attempt_token
            or envelope.result.job_id != job.job_id
            or envelope.result.attempt_token != job.attempt_token
            or envelope.payload.get("job_uid") != job.job_id
            or envelope.payload.get("attempt_token") != job.attempt_token
            or envelope.result.should_retry
        ):
            raise StorageError("delivery envelope identity or terminal status does not match")
        return envelope

    def find(self, job: Job) -> tuple[str, DeliveryEnvelope] | None:
        # Never cache a tokenless source by job_id: API judge retries reuse it.
        if job.attempt_token is None:
            return None
        names = self._storage.list_prefix(job.job_id, attempt_prefix(job), LOOKUP_LIMIT)
        if not names:
            return None
        artifact = names[0]
        return artifact, self.load(job, artifact)

    def persist(self, lease: Lease, result: JobResult) -> tuple[str, DeliveryEnvelope]:
        now = time.time()
        artifact = f"{attempt_prefix(lease.job)}{time.time_ns()}-{uuid.uuid4().hex}.json"
        envelope = DeliveryEnvelope(
            job=lease.job,
            result=result.model_copy(update={"report": None}),
            payload=self._plane.result_payload(result),
            target_tier=lease.queue_url,
            source_attempt=lease.delivery_attempt,
            created_at=now,
            deadline=now + self._config.result_delivery_deadline_seconds,
            max_rounds=self._config.result_delivery_max_rounds,
        )
        self._storage.put_immutable(lease.job.job_id, artifact, envelope.model_dump_json().encode())
        self._log.info(
            "result.persisted",
            job_id=result.job_id,
            attempt_token=result.attempt_token,
            artifact=artifact,
        )
        return artifact, envelope

    def state(self, lease: Lease, artifact: str, envelope: DeliveryEnvelope) -> DeliveryState:
        if "delivery" in lease.job.metadata:
            try:
                return DeliveryState.model_validate(lease.job.metadata["delivery"])
            except ValidationError as exc:
                raise StorageError(f"invalid delivery marker: {exc}") from exc
        return DeliveryState(
            artifact=artifact,
            round=max(0, lease.delivery_attempt - envelope.source_attempt),
            deadline=envelope.deadline,
            max_rounds=envelope.max_rounds,
        )

    @staticmethod
    def message(envelope: DeliveryEnvelope, state: DeliveryState) -> Job:
        # Carry only a reference, never a report, in SQS. Legacy job-shaped body
        # remains parseable during rollout, but all fleet readers must be upgraded.
        return envelope.job.model_copy(
            update={
                "metadata": {**envelope.job.metadata, "delivery": state.model_dump(mode="json")}
            }
        )

    def deliver(self, lease: Lease, artifact: str, envelope: DeliveryEnvelope) -> JobResult:
        state = self.state(lease, artifact, envelope)
        # Each receive of a successor counts, including restarts/send failures.
        attempt = state.round + (lease.delivery_attempt if "delivery" in lease.job.metadata else 1)
        maximum = min(state.max_rounds, self._config.result_delivery_max_rounds)
        now = time.time()
        exhausted = state.exhausted or now >= state.deadline or attempt > maximum
        if not exhausted and self._plane.publish_payload(envelope.payload):
            self._queue.complete(lease)
            self._log.info(
                "result.delivered",
                job_id=lease.job.job_id,
                artifact=artifact,
                attempt_token=lease.job.attempt_token,
                round=attempt,
            )
            return envelope.result

        delay = min(300, 2 ** min(attempt, 9))
        exhausted = exhausted or attempt >= maximum or now + delay >= state.deadline
        successor = state.model_copy(update={"round": attempt, "exhausted": exhausted})
        if exhausted:
            self._log.error(
                "result.recovery_required",
                job_id=lease.job.job_id,
                attempt_token=lease.job.attempt_token,
                artifact=artifact,
                round=attempt,
                deadline=state.deadline,
                target_tier=envelope.target_tier,
            )
        # Publish first. If publication (or the later delete) fails, source stays
        # live and its next receive finds the same immutable callback.
        self._queue.publish(
            self.message(envelope, successor),
            target_tier=envelope.target_tier,
            delay_seconds=0 if exhausted else delay,
            dead_letter=exhausted,
        )
        self._queue.complete(lease)
        return envelope.result

    def recover(self, job_id: str, artifact: str, *, dry_run: bool = True) -> dict[str, Any]:
        """Inspect/requeue one exact key. Explicit recovery starts one new bounded window."""
        try:
            envelope = DeliveryEnvelope.model_validate_json(
                self._storage.get_bytes(job_id, artifact)
            )
        except ValidationError as exc:
            raise StorageError(f"invalid delivery envelope: {exc}") from exc
        # Use the same identity/path checks as automatic replay.
        envelope = self.load(envelope.job, artifact)
        if envelope.job.job_id != job_id:
            raise StorageError("recovery job_id does not match envelope")
        state = DeliveryState(
            artifact=artifact,
            round=0,
            deadline=time.time() + self._config.result_delivery_deadline_seconds,
            max_rounds=self._config.result_delivery_max_rounds,
        )
        if not dry_run:
            if not self._plane.enabled:
                raise StorageError("enable control plane before recovery")
            self._queue.publish(self.message(envelope, state), target_tier=envelope.target_tier)
            self._log.info(
                "result.recovery_enqueued",
                job_id=job_id,
                artifact=artifact,
                attempt_token=envelope.job.attempt_token,
                deadline=state.deadline,
            )
        return {
            "job_id": job_id,
            "artifact": artifact,
            "dry_run": dry_run,
            "attempt_token": envelope.job.attempt_token,
            "target_tier": envelope.target_tier,
            "deadline": state.deadline,
            "max_rounds": state.max_rounds,
        }
