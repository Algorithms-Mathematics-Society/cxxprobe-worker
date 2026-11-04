"""Crash/restart and failure-injection proof for durable callback delivery."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from cxxprobe_worker.config import ControlPlaneConfig, WorkerConfig
from cxxprobe_worker.control_plane import ControlPlaneClient
from cxxprobe_worker.daemon import Worker
from cxxprobe_worker.delivery import DeliveryState, ResultDelivery, attempt_prefix
from cxxprobe_worker.jobs import Job, JobResult, JobStatus
from cxxprobe_worker.monitoring import HealthReporter, Logger, Metrics
from cxxprobe_worker.queue import Lease, LocalJobQueue, QueueError
from cxxprobe_worker.storage import FilesystemArtifactStorage, StorageError


@dataclass
class Rig:
    config: WorkerConfig
    queue: LocalJobQueue
    storage: FilesystemArtifactStorage
    logger: Logger
    now: float
    executor: Any = field(default_factory=Mock)
    payloads: list[dict[str, Any]] = field(default_factory=list)
    status: int = 500
    lost_response: bool = False
    plane: ControlPlaneClient = field(init=False)
    worker: Worker = field(init=False)
    job: Job = field(init=False)

    def __post_init__(self) -> None:
        self.config.result_delivery_enabled = True
        self.config.control_plane = ControlPlaneConfig(base_url="https://api.example.test")
        self.job = Job(
            job_id="j1",
            package_path="p",
            submission_path="s",
            metadata={"attempt_token": str(uuid4())},
        )
        self.executor.execute.return_value = JobResult(
            job_id="j1",
            status=JobStatus.SUCCEEDED,
            report={"overall": "PASS"},
            artifacts={"report.json": "original-report"},
        )
        self.restart()

    def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/workers/result":
            self.payloads.append(json.loads(request.content))
            if self.lost_response:
                raise httpx.ReadTimeout("accepted callback but response lost")
            return httpx.Response(self.status)
        return httpx.Response(200, json={"worker_uid": "worker-new"})

    def restart(self) -> None:
        self.plane = ControlPlaneClient(self.config.control_plane, self.logger)
        self.plane._worker_uid = "worker-original" if not self.payloads else "worker-new"
        self.plane._client = httpx.Client(
            base_url=self.config.control_plane.base_url, transport=httpx.MockTransport(self.respond)
        )
        metrics = Metrics()
        self.worker = Worker(
            self.config,
            self.queue,
            self.executor,
            self.logger,
            metrics,
            HealthReporter(None, "w", metrics),
            control_plane=self.plane,
            storage=self.storage,
        )

    def run_source(self) -> JobResult | None:
        self.queue.publish(self.job)
        return self.worker.run_once()

    def advance(self, seconds: int = 301) -> None:
        self.now += seconds

    def artifacts(self) -> list[str]:
        return self.storage.list_prefix("j1", attempt_prefix(self.job), 32)

    def delivery(self) -> ResultDelivery:
        return ResultDelivery(self.config, self.storage, self.queue, self.plane, self.logger)


@pytest.fixture
def rig(config, job_queue, storage, logger, monkeypatch) -> Rig:
    result = Rig(config, job_queue, storage, logger, time.time())
    monkeypatch.setattr(time, "time", lambda: result.now)
    return result


def test_restart_replays_completed_callback_without_rejudging(rig: Rig):
    rig.run_source()
    rig.restart()
    rig.run_source()  # duplicate original, not the delayed successor
    assert rig.executor.execute.call_count == 1
    assert rig.payloads[0] == rig.payloads[1]
    assert rig.payloads[1]["attempt_token"] == rig.job.attempt_token
    assert rig.payloads[1]["worker_uid"] == "worker-original"


def test_lost_response_replays_identical_callback_on_last_source_delivery(rig: Rig):
    rig.lost_response = True
    rig.queue.publish(rig.job)
    lease = rig.queue.claim()
    assert lease is not None
    rig.worker._process_lease(Lease(rig.job, lease.receipt, delivery_attempt=5))
    assert rig.queue.inflight_count() == 0
    assert rig.queue.depth() == 1
    rig.restart()
    rig.lost_response = False
    rig.status = 204  # API acknowledges duplicate or known retired token
    rig.advance()
    rig.worker.run_once()
    assert len(rig.payloads) == 2
    assert rig.payloads[0] == rig.payloads[1]
    assert rig.executor.execute.call_count == 1
    assert rig.queue.depth() == rig.queue.inflight_count() == 0


def test_source_delete_failure_then_rollback_replays_cache(rig: Rig, monkeypatch):
    rig.status = 204
    real_complete = rig.queue.complete
    monkeypatch.setattr(rig.queue, "complete", Mock(side_effect=QueueError("delete failed")))
    rig.run_source()
    assert rig.queue.inflight_count() == 1
    assert len(rig.artifacts()) == 1
    monkeypatch.setattr(rig.queue, "complete", real_complete)
    rig.config.result_delivery_enabled = False
    rig.advance(601)
    rig.restart()
    rig.worker.run_once()
    assert rig.executor.execute.call_count == 1
    assert rig.payloads[0] == rig.payloads[1]
    assert rig.queue.inflight_count() == 0


def test_marker_reader_survives_rollback_and_forwards_original_token(rig: Rig):
    rig.run_source()
    rig.config.result_delivery_enabled = False
    rig.status = 204
    rig.restart()
    rig.advance()
    rig.worker.run_once()
    assert rig.payloads[0] == rig.payloads[1]
    assert rig.executor.execute.call_count == 1
    assert rig.queue.depth() == 0


def test_fresh_attempt_for_same_job_executes_instead_of_replaying_old_failure(rig: Rig):
    rig.executor.execute.return_value = JobResult(
        job_id="j1", status=JobStatus.FAILED, error="old permanent failure"
    )
    rig.run_source()
    old_token = rig.job.attempt_token
    rig.job = rig.job.model_copy(update={"metadata": {"attempt_token": str(uuid4())}})
    rig.status = 204
    rig.run_source()
    assert rig.executor.execute.call_count == 2
    assert rig.payloads[0]["attempt_token"] == old_token
    assert rig.payloads[1]["attempt_token"] == rig.job.attempt_token


def test_tokenless_jobs_keep_legacy_behavior_even_with_production_enabled(rig: Rig):
    rig.job = rig.job.model_copy(update={"metadata": {}})
    rig.run_source()
    rig.worker.run_once()
    assert rig.executor.execute.call_count == 2
    assert "attempt_token" not in rig.payloads[0]
    assert rig.storage.list_artifacts("j1") == []


def test_disabled_production_forwards_token_but_does_not_persist(rig: Rig):
    rig.config.result_delivery_enabled = False
    rig.status = 204
    rig.run_source()
    assert rig.payloads[0]["attempt_token"] == rig.job.attempt_token
    assert rig.storage.list_artifacts("j1") == []


@pytest.mark.parametrize("operation", ["put_immutable", "list_prefix"])
def test_storage_failure_does_not_upload_or_ack(rig: Rig, monkeypatch, operation: str):
    monkeypatch.setattr(rig.storage, operation, Mock(side_effect=StorageError("unavailable")))
    rig.run_source()
    assert not rig.payloads
    assert rig.queue.inflight_count() == 1
    assert rig.queue.depth() == 0
    assert rig.executor.execute.call_count == (1 if operation == "put_immutable" else 0)


def test_successor_send_failure_retains_source_and_restarts_without_judging(rig: Rig, monkeypatch):
    real_publish = rig.queue.publish
    rig.queue.publish(rig.job)
    monkeypatch.setattr(rig.queue, "publish", Mock(side_effect=QueueError("send failed")))
    rig.worker.run_once()
    assert rig.queue.inflight_count() == 1
    assert len(rig.artifacts()) == 1
    monkeypatch.setattr(rig.queue, "publish", real_publish)
    rig.restart()
    rig.status = 204
    rig.advance(601)
    rig.worker.run_once()
    assert rig.executor.execute.call_count == 1
    assert rig.queue.inflight_count() == 0


def test_send_success_delete_failure_creates_only_duplicate_replays(rig: Rig, monkeypatch):
    real_complete = rig.queue.complete
    monkeypatch.setattr(rig.queue, "complete", Mock(side_effect=QueueError("delete failed")))
    rig.run_source()
    assert rig.queue.depth() == rig.queue.inflight_count() == 1
    monkeypatch.setattr(rig.queue, "complete", real_complete)
    rig.status = 204
    rig.advance(601)
    rig.restart()
    rig.worker.run_once()
    rig.worker.run_once()
    assert rig.executor.execute.call_count == 1
    assert len(rig.payloads) == 3
    assert all(payload == rig.payloads[0] for payload in rig.payloads)


def test_exhausted_delivery_retains_envelope_and_dead_letters_reference(rig: Rig):
    rig.config.result_delivery_max_rounds = 2
    rig.run_source()
    assert rig.worker.run_once() is None  # backoff prevents an immediate hot loop
    rig.advance(3)
    rig.worker.run_once()
    assert len(rig.payloads) == 2
    assert rig.queue.dead_letter_count() == 1
    assert rig.queue.depth() == 0
    artifact = rig.artifacts()[0]
    dead = Job.model_validate_json(next((rig.queue.root / "dead").glob("*.json")).read_bytes())
    assert dead.metadata["delivery"]["artifact"] == artifact
    assert dead.metadata["delivery"]["exhausted"]
    assert rig.storage.get_bytes("j1", artifact)
    assert rig.executor.execute.call_count == 1


def test_fixed_deadline_is_preserved_and_prevents_more_uploads(rig: Rig):
    rig.config.result_delivery_deadline_seconds = 5
    rig.run_source()
    first = rig.delivery().load(rig.job, rig.artifacts()[0])
    rig.advance(6)
    rig.worker.run_once()
    assert len(rig.payloads) == 1
    assert rig.queue.dead_letter_count() == 1
    dead = Job.model_validate_json(next((rig.queue.root / "dead").glob("*.json")).read_bytes())
    assert dead.metadata["delivery"]["deadline"] == first.deadline


def test_unacknowledged_marker_receives_count_toward_budget(rig: Rig, monkeypatch):
    rig.config.result_delivery_max_rounds = 3
    rig.run_source()
    rig.advance(3)
    real_publish = rig.queue.publish
    monkeypatch.setattr(rig.queue, "publish", Mock(side_effect=QueueError("send failed")))
    rig.worker.run_once()  # round 2, send failed
    monkeypatch.setattr(rig.queue, "publish", real_publish)
    rig.advance(61)  # local fixture visibility=60
    rig.worker.run_once()  # same marker receive 2 -> round 3 -> DLQ
    assert len(rig.payloads) == 3
    assert rig.queue.dead_letter_count() == 1
    assert rig.executor.execute.call_count == 1


def test_exhaustion_dlq_publish_failure_does_not_ack(rig: Rig, monkeypatch):
    rig.config.result_delivery_max_rounds = 1
    real_publish = rig.queue.publish
    rig.queue.publish(rig.job)
    monkeypatch.setattr(rig.queue, "publish", Mock(side_effect=QueueError("DLQ send failed")))
    rig.worker.run_once()
    assert rig.queue.inflight_count() == 1
    assert rig.artifacts()
    monkeypatch.setattr(rig.queue, "publish", real_publish)


@pytest.mark.parametrize("marker", [{}, "not a marker", {"artifact": "../report.json"}])
def test_malformed_marker_never_executes_or_acks(rig: Rig, marker):
    rig.job.metadata["delivery"] = marker
    rig.run_source()
    assert rig.executor.execute.call_count == 0
    assert rig.queue.inflight_count() == 1


def test_marker_without_control_plane_never_executes_or_acks(rig: Rig):
    rig.run_source()
    rig.config.control_plane.base_url = ""
    rig.restart()
    rig.advance()
    rig.worker.run_once()
    assert rig.executor.execute.call_count == 1
    assert rig.queue.inflight_count() == 1


def test_missing_referenced_envelope_never_executes_or_acks(rig: Rig):
    rig.job.metadata["delivery"] = DeliveryState(
        artifact=attempt_prefix(rig.job) + "missing.json",
        round=1,
        deadline=rig.now + 300,
        max_rounds=5,
    ).model_dump()
    rig.run_source()
    assert rig.executor.execute.call_count == 0
    assert rig.queue.inflight_count() == 1


def test_recovery_is_exact_key_dry_run_then_new_bounded_window(rig: Rig):
    rig.config.result_delivery_max_rounds = 1
    rig.run_source()
    artifact = rig.artifacts()[0]
    original = rig.storage.get_bytes("j1", artifact)
    recovery = rig.delivery().recover("j1", artifact)
    assert recovery["dry_run"]
    assert rig.queue.depth() == 0
    rig.advance(4000)
    rig.delivery().recover("j1", artifact, dry_run=False)
    rig.status = 204
    rig.worker.run_once()
    assert rig.payloads[0] == rig.payloads[1]
    assert rig.storage.get_bytes("j1", artifact) == original
    assert rig.executor.execute.call_count == 1
    with pytest.raises(StorageError):
        rig.delivery().recover("another-job", artifact)


def test_final_preparation_failure_is_persisted_but_transient_execution_is_not(rig: Rig):
    rig.executor.execute.return_value = JobResult(
        job_id="j1", status=JobStatus.RETRYABLE, error="sandbox unavailable"
    )
    rig.run_source()
    assert not rig.artifacts()
    assert not rig.payloads
    for _ in range(4):
        rig.worker.run_once()
    assert len(rig.artifacts()) == 1
    assert rig.payloads[0]["verdict"] == "SE"
    assert "after 5 attempts" in rig.payloads[0]["error"]
    assert rig.queue.dead_letter_count() == 0  # successor still has its own delivery budget
    rig.advance()
    rig.status = 204
    rig.worker.run_once()
    assert rig.executor.execute.call_count == 5


def test_report_is_not_duplicated_in_saved_envelope_and_first_result_keeps_it(rig: Rig):
    output = "unique candidate output " * 10000
    report = {"tests": {"manual": {"cases": [{"verdict": "RE", "stdout": output}]}}}
    rig.executor.execute.return_value = JobResult(
        job_id="j1", status=JobStatus.FAILED, report=report, error="runtime crash"
    )
    result = rig.run_source()
    assert result is not None and result.report == report
    saved = rig.storage.get_bytes("j1", rig.artifacts()[0])
    decoded = json.loads(saved)
    assert decoded["result"]["report"] is None
    assert decoded["payload"]["testcases"][0]["stdout"] == output
    assert saved.count(output.encode()) == 1
    assert decoded["payload"]["verdict"] == "RE"
    assert decoded["payload"]["error"] == ""  # real verdict vs synthetic judge failure


@pytest.mark.parametrize("token", ["", "not-a-uuid", 5, True])
def test_malformed_tokens_are_rejected(token):
    with pytest.raises(ValidationError):
        Job(job_id="j", package_path="p", submission_path="s", metadata={"attempt_token": token})


def test_uuid_tokens_are_canonicalized_without_losing_identity():
    token = uuid4()
    job = Job(
        job_id="j",
        package_path="p",
        submission_path="s",
        metadata={"attempt_token": token.hex.upper()},
    )
    assert job.attempt_token == str(token)


def test_standalone_mode_is_preserved_when_production_flag_is_on(rig: Rig):
    rig.config.control_plane.base_url = ""
    rig.restart()
    result = rig.run_source()
    assert result is not None and result.ok
    assert rig.queue.depth() == 0
    assert rig.storage.list_artifacts("j1") == []
    assert not rig.payloads


def test_early_permanent_preparation_failure_is_saved(rig: Rig):
    rig.executor.execute.return_value = JobResult(
        job_id="j1", status=JobStatus.FAILED, error="submission not found"
    )
    rig.run_source()
    assert len(rig.artifacts()) == 1
    assert rig.payloads[0]["verdict"] == "SE"
    assert rig.payloads[0]["error"] == "submission not found"
    rig.restart()
    rig.status = 204
    rig.advance()
    rig.worker.run_once()
    assert rig.executor.execute.call_count == 1


def test_lookup_limit_failure_does_not_fall_back_to_execution(rig: Rig):
    prefix = attempt_prefix(rig.job)
    for i in range(33):
        rig.storage.put_immutable("j1", f"{prefix}{i}.json", b"{}")
    rig.run_source()
    assert rig.executor.execute.call_count == 0
    assert not rig.payloads
    assert rig.queue.inflight_count() == 1


def test_recovery_required_log_includes_exact_key_on_exhaustion(rig: Rig, caplog):
    rig.config.result_delivery_max_rounds = 1
    rig.run_source()
    record = next(
        record for record in caplog.records if record.message == "result.recovery_required"
    )
    assert record.fields["artifact"] == rig.artifacts()[0]
    assert record.fields["job_id"] == "j1"
    assert record.fields["attempt_token"] == rig.job.attempt_token


def test_stale_known_retired_token_acknowledged_and_unknown_token_409_not_acked(rig: Rig):
    rig.status = 409
    rig.run_source()
    assert rig.queue.depth() == 1
    rig.status = 204
    rig.restart()
    rig.advance()
    rig.worker.run_once()
    assert rig.payloads[0]["attempt_token"] == rig.payloads[1]["attempt_token"]
    assert rig.queue.depth() == 0
    assert rig.executor.execute.call_count == 1
