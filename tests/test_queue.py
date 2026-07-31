from __future__ import annotations

import time
from pathlib import Path

import pytest

from cxxprobe_worker.jobs import Job
from cxxprobe_worker.queue import IJobQueue, LocalJobQueue, build_queue


def make_job(job_id: str = "job-1", tmp_path: Path | None = None) -> Job:
    base = tmp_path or Path("/tmp")
    return Job(
        job_id=job_id, package_path=str(base / "pkg.zip"), submission_path=str(base / "sub.cpp")
    )


def test_publish_then_claim_returns_the_job(job_queue: LocalJobQueue, tmp_path: Path):
    job_queue.publish(make_job("job-1", tmp_path))
    lease = job_queue.claim()
    assert lease is not None
    assert lease.job.job_id == "job-1"


def test_claim_on_empty_queue_returns_none(job_queue: LocalJobQueue):
    assert job_queue.claim() is None


def test_claimed_job_is_not_handed_out_twice(job_queue: LocalJobQueue, tmp_path: Path):
    job_queue.publish(make_job("job-1", tmp_path))
    assert job_queue.claim() is not None
    assert job_queue.claim() is None


def test_complete_removes_the_job_permanently(job_queue: LocalJobQueue, tmp_path: Path):
    job_queue.publish(make_job("job-1", tmp_path))
    lease = job_queue.claim()
    assert lease is not None
    job_queue.complete(lease)
    assert job_queue.depth() == 0
    assert job_queue.inflight_count() == 0
    assert job_queue.claim() is None


def test_release_makes_the_job_claimable_again(job_queue: LocalJobQueue, tmp_path: Path):
    job_queue.publish(make_job("job-1", tmp_path))
    lease = job_queue.claim()
    assert lease is not None
    job_queue.release(lease)
    assert job_queue.depth() == 1
    again = job_queue.claim()
    assert again is not None
    assert again.job.job_id == "job-1"


def test_depth_counts_only_pending_jobs(job_queue: LocalJobQueue, tmp_path: Path):
    job_queue.publish(make_job("job-1", tmp_path))
    job_queue.publish(make_job("job-2", tmp_path))
    assert job_queue.depth() == 2
    job_queue.claim()
    assert job_queue.depth() == 1
    assert job_queue.inflight_count() == 1


def test_jobs_are_claimed_in_publish_order(job_queue: LocalJobQueue, tmp_path: Path):
    for i in range(3):
        job_queue.publish(make_job(f"job-{i}", tmp_path))
        time.sleep(0.002)  # filenames are millisecond-prefixed
    seen = []
    while (lease := job_queue.claim()) is not None:
        seen.append(lease.job.job_id)
    assert seen == ["job-0", "job-1", "job-2"]


def test_expired_lease_is_reclaimed(tmp_path: Path):
    # A worker killed mid-job leaves nothing behind that knows the job
    # existed — the visibility timeout is the only path back.
    q = LocalJobQueue(tmp_path / "q", visibility_timeout_seconds=0.0)
    q.publish(make_job("job-1", tmp_path))
    first = q.claim()
    assert first is not None
    time.sleep(0.01)
    second = q.claim()
    assert second is not None
    assert second.job.job_id == "job-1"


def test_unexpired_lease_is_not_reclaimed(tmp_path: Path):
    q = LocalJobQueue(tmp_path / "q", visibility_timeout_seconds=3600.0)
    q.publish(make_job("job-1", tmp_path))
    assert q.claim() is not None
    assert q.claim() is None


def test_malformed_job_is_quarantined_not_redelivered(job_queue: LocalJobQueue):
    pending = job_queue.root / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "0-bad-job.json").write_text("{ not json")

    # An unparseable job must leave the queue, or every poll rediscovers it.
    assert job_queue.claim() is None
    assert job_queue.depth() == 0
    assert list((job_queue.root / "dead").glob("*.json"))


def test_job_missing_required_fields_is_quarantined(job_queue: LocalJobQueue):
    pending = job_queue.root / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "0-incomplete.json").write_text('{"job_id": "x"}')
    assert job_queue.claim() is None
    assert list((job_queue.root / "dead").glob("*.json"))


def test_metadata_survives_a_round_trip(job_queue: LocalJobQueue, tmp_path: Path):
    job = Job(
        job_id="job-1",
        package_path=str(tmp_path / "p.zip"),
        submission_path=str(tmp_path / "s.cpp"),
        problem_slug="a-warmup",
        metadata={"submission_id": "abc123", "attempt": 2},
    )
    job_queue.publish(job)
    lease = job_queue.claim()
    assert lease is not None
    assert lease.job.problem_slug == "a-warmup"
    assert lease.job.metadata == {"submission_id": "abc123", "attempt": 2}


def test_release_of_an_already_completed_lease_is_a_noop(job_queue: LocalJobQueue, tmp_path: Path):
    job_queue.publish(make_job("job-1", tmp_path))
    lease = job_queue.claim()
    assert lease is not None
    job_queue.complete(lease)
    job_queue.release(lease)  # must not resurrect it
    assert job_queue.depth() == 0


def test_build_queue_returns_local_backend(tmp_path: Path):
    built = build_queue("local", tmp_path, 60.0)
    assert isinstance(built, LocalJobQueue)
    assert isinstance(built, IJobQueue)


def test_build_queue_rejects_unknown_backend(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown queue backend"):
        build_queue("kafka", tmp_path, 60.0)


def test_build_queue_requires_a_url_for_sqs(tmp_path: Path):
    with pytest.raises(ValueError, match=r"queue\.queue_url is required"):
        build_queue("sqs", tmp_path, 60.0, queue_url="")


def test_delivery_attempts_increment_across_redeliveries(tmp_path: Path):
    q = LocalJobQueue(tmp_path / "q", visibility_timeout_seconds=3600.0, max_delivery_attempts=5)
    q.publish(make_job("job-1", tmp_path))

    for expected in (1, 2, 3):
        lease = q.claim()
        assert lease is not None
        assert lease.delivery_attempt == expected
        q.release(lease)


def test_job_is_dead_lettered_after_the_attempt_cap(tmp_path: Path):
    # A retryable classification can't tell "bad host" from "bad package" on
    # the first attempt, so without a cap a broken job recirculates for ever.
    q = LocalJobQueue(tmp_path / "q", visibility_timeout_seconds=3600.0, max_delivery_attempts=2)
    q.publish(make_job("job-1", tmp_path))

    first = q.claim()
    assert first is not None
    q.release(first)
    second = q.claim()
    assert second is not None
    q.release(second)

    assert q.claim() is None
    assert q.depth() == 0
    assert q.dead_letter_count() == 1


def test_completing_a_job_stops_the_attempt_count_growing(tmp_path: Path):
    q = LocalJobQueue(tmp_path / "q", visibility_timeout_seconds=3600.0, max_delivery_attempts=2)
    q.publish(make_job("job-1", tmp_path))
    lease = q.claim()
    assert lease is not None
    q.complete(lease)
    assert q.dead_letter_count() == 0
    assert q.depth() == 0
