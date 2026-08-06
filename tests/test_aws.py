"""S3 and SQS backends, against moto's in-process AWS.

These run everywhere with no credentials and no network. They cover the
contract — the same one `test_storage.py` and `test_queue.py` assert for the
filesystem backends — rather than re-testing boto3.
"""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from cxxprobe_worker.aws.fetch import FetchError, S3Fetcher, is_remote, parse_s3_uri
from cxxprobe_worker.aws.queue import SqsJobQueue
from cxxprobe_worker.aws.storage import S3ArtifactStorage
from cxxprobe_worker.jobs import Job
from cxxprobe_worker.queue import IJobQueue
from cxxprobe_worker.storage import IArtifactStorage, StorageError

REGION = "ap-south-1"
BUCKET = "ams-test-objects"


@pytest.fixture
def s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
        )
        yield client


@pytest.fixture
def storage(s3_client) -> S3ArtifactStorage:
    return S3ArtifactStorage(bucket=BUCKET, prefix="artifacts", client=s3_client)


@pytest.fixture
def sqs_queue():
    with mock_aws():
        client = boto3.client("sqs", region_name=REGION)
        url = client.create_queue(QueueName="test-eval")["QueueUrl"]
        yield SqsJobQueue(url, wait_time_seconds=0, client=client), client


def make_job(job_id: str = "job-1") -> Job:
    return Job(
        job_id=job_id,
        package_path="s3://bucket/problems/p/v/package.cxxpkg",
        submission_path="s3://bucket/submissions/s/source.cpp",
    )


# ── S3 storage ────────────────────────────────────────────────────────────


def test_satisfies_the_storage_protocol(storage: S3ArtifactStorage):
    assert isinstance(storage, IArtifactStorage)


def test_put_and_get_text_round_trips(storage: S3ArtifactStorage):
    storage.put_text("job-1", "report.json", '{"ok": true}')
    assert storage.get_bytes("job-1", "report.json") == b'{"ok": true}'


def test_locator_is_an_s3_uri(storage: S3ArtifactStorage):
    locator = storage.put_text("job-1", "report.json", "{}")
    assert locator == f"s3://{BUCKET}/artifacts/job-1/report.json"


def test_put_file_uploads_contents(storage: S3ArtifactStorage, tmp_path: Path):
    src = tmp_path / "sub.cpp"
    src.write_text("int main(){}\n")
    storage.put_file("job-1", "submission.cpp", src)
    assert storage.get_bytes("job-1", "submission.cpp") == b"int main(){}\n"


def test_exists_reflects_stored_state(storage: S3ArtifactStorage):
    assert storage.exists("job-1", "a.txt") is False
    storage.put_text("job-1", "a.txt", "x")
    assert storage.exists("job-1", "a.txt") is True


def test_list_artifacts_is_sorted_and_scoped_per_job(storage: S3ArtifactStorage):
    storage.put_text("job-1", "b.txt", "b")
    storage.put_text("job-1", "a.txt", "a")
    storage.put_text("job-2", "c.txt", "c")
    assert storage.list_artifacts("job-1") == ["a.txt", "b.txt"]
    assert storage.list_artifacts("job-2") == ["c.txt"]


def test_list_artifacts_of_unknown_job_is_empty(storage: S3ArtifactStorage):
    assert storage.list_artifacts("never-existed") == []


@pytest.mark.parametrize("name", ["../escape.txt", "a/../../escape.txt", "/etc/passwd", ""])
def test_traversal_is_rejected_the_same_as_on_disk(storage: S3ArtifactStorage, name: str):
    # A job id and artifact name can both come from a queue message, so the
    # S3 backend must reject exactly what the filesystem one does.
    with pytest.raises(StorageError):
        storage.put_text("job-1", name, "x")


def test_reading_a_missing_artifact_raises(storage: S3ArtifactStorage):
    with pytest.raises(StorageError):
        storage.get_bytes("job-1", "nope.txt")


# ── S3 fetch ──────────────────────────────────────────────────────────────


def test_is_remote_distinguishes_s3_from_paths():
    assert is_remote("s3://bucket/key") is True
    assert is_remote("/var/lib/package.zip") is False
    assert is_remote(Path("/tmp/x")) is False


def test_parse_s3_uri_splits_bucket_and_key():
    assert parse_s3_uri("s3://b/a/c.txt") == ("b", "a/c.txt")


@pytest.mark.parametrize("bad", ["https://example.com/x", "s3://bucket", "s3:///key"])
def test_parse_s3_uri_rejects_malformed(bad: str):
    with pytest.raises(FetchError):
        parse_s3_uri(bad)


def test_fetch_downloads_to_the_destination(s3_client, tmp_path: Path):
    s3_client.put_object(Bucket=BUCKET, Key="problems/p/package.cxxpkg", Body=b"PKGDATA")
    fetcher = S3Fetcher(client=s3_client)
    dest = tmp_path / "nested" / "package.zip"

    out = fetcher.fetch(f"s3://{BUCKET}/problems/p/package.cxxpkg", dest)
    assert out == dest
    assert dest.read_bytes() == b"PKGDATA"


def test_fetch_of_a_missing_key_raises(s3_client, tmp_path: Path):
    fetcher = S3Fetcher(client=s3_client)
    with pytest.raises(FetchError):
        fetcher.fetch(f"s3://{BUCKET}/nope", tmp_path / "x")


# ── SQS queue ─────────────────────────────────────────────────────────────


def test_satisfies_the_queue_protocol(sqs_queue):
    queue, _ = sqs_queue
    assert isinstance(queue, IJobQueue)


def test_publish_then_claim_returns_the_job(sqs_queue):
    queue, _ = sqs_queue
    queue.publish(make_job("job-1"))
    lease = queue.claim()
    assert lease is not None
    assert lease.job.job_id == "job-1"


def test_claim_on_empty_queue_returns_none(sqs_queue):
    queue, _ = sqs_queue
    assert queue.claim() is None


def test_complete_removes_the_message(sqs_queue):
    queue, _ = sqs_queue
    queue.publish(make_job("job-1"))
    lease = queue.claim()
    assert lease is not None
    queue.complete(lease)
    assert queue.claim() is None


def test_release_holds_the_job_back_instead_of_respinning_it(sqs_queue):
    """A released job must not come straight back.

    Zero visibility turns a fast-failing job — an unreadable package, say —
    into a hot loop: claim, fail in 0.1s, release, repeat several times a
    second until the DLQ catches it, starving real submissions meanwhile.
    """
    queue, _ = sqs_queue
    queue.publish(make_job("job-1"))
    lease = queue.claim()
    assert lease is not None
    queue.release(lease)

    assert queue.claim() is None, "the job should be invisible during backoff"


def test_backoff_doubles_per_attempt_and_is_capped(sqs_queue):
    queue, _ = sqs_queue
    assert [queue._backoff_seconds(n) for n in (1, 2, 3, 4)] == [2, 4, 8, 16]
    # Never longer than a running job's visibility timeout, or a released job
    # would sit invisible longer than one actually being judged.
    assert queue._backoff_seconds(50) == 300


def test_delivery_attempt_comes_from_sqs_not_the_worker(sqs_queue):
    # The local queue tracks attempts in the filename; SQS reports
    # ApproximateReceiveCount, so the worker must read rather than count.
    queue, client = sqs_queue
    queue.publish(make_job("job-1"))

    first = queue.claim()
    assert first is not None
    assert first.delivery_attempt == 1
    # Release with no backoff so the assertion is about counting, not timing.
    client.change_message_visibility(
        QueueUrl=queue.queue_url, ReceiptHandle=first.receipt, VisibilityTimeout=0
    )

    second = queue.claim()
    assert second is not None
    assert second.delivery_attempt == 2


def test_metadata_survives_a_round_trip(sqs_queue):
    queue, _ = sqs_queue
    job = Job(
        job_id="job-1",
        package_path="s3://b/p.cxxpkg",
        submission_path="s3://b/s.cpp",
        problem_slug="a-warmup",
        metadata={"submission_uid": "abc", "contest_uid": "def"},
    )
    queue.publish(job)
    lease = queue.claim()
    assert lease is not None
    assert lease.job.metadata == {"submission_uid": "abc", "contest_uid": "def"}
    assert lease.job.package_path == "s3://b/p.cxxpkg"


def test_poison_message_is_deleted_not_redelivered(sqs_queue):
    # A body that never parses would otherwise be reclaimed every poll
    # forever — SQS's DLQ only counts receives, not parse failures.
    queue, client = sqs_queue
    client.send_message(QueueUrl=queue.queue_url, MessageBody="{ not json")

    assert queue.claim() is None
    assert queue.claim() is None


def test_job_missing_required_fields_is_dropped(sqs_queue):
    queue, client = sqs_queue
    client.send_message(QueueUrl=queue.queue_url, MessageBody=json.dumps({"job_id": "x"}))
    assert queue.claim() is None


def test_depth_reports_queued_messages(sqs_queue):
    queue, _ = sqs_queue
    assert queue.depth() == 0
    queue.publish(make_job("job-1"))
    queue.publish(make_job("job-2"))
    assert queue.depth() == 2


# ── regression: an s3:// URI must survive the Job model ───────────────────


def test_s3_uri_survives_the_job_model():
    """`Path("s3://b/k")` collapses the double slash to `s3:/b/k`.

    This shipped once: package_path/submission_path were typed `Path`, so by
    the time the executor asked `is_remote()` the URI had already been
    corrupted into something that no longer parsed, and every cloud job
    failed with "submission not found: s3:/...".
    """
    job = Job(
        job_id="j1",
        package_path="s3://ams-prod-objects/problems/p/v/package.cxxpkg",
        submission_path="s3://ams-prod-objects/submissions/s/source.cpp",
    )
    assert job.package_path == "s3://ams-prod-objects/problems/p/v/package.cxxpkg"
    assert is_remote(job.package_path)
    assert parse_s3_uri(job.submission_path) == (
        "ams-prod-objects",
        "submissions/s/source.cpp",
    )


def test_a_path_argument_is_still_accepted_and_normalised():
    """Local callers hold a Path; they shouldn't have to stringify."""
    # Passing Path is the whole point here, so the type errors are expected.
    job = Job(
        job_id="j1",
        package_path=Path("/tmp/pkg"),  # type: ignore[arg-type]
        submission_path=Path("/tmp/s.cpp"),  # type: ignore[arg-type]
    )
    assert job.package_path == "/tmp/pkg"
    assert isinstance(job.package_path, str)
    assert not is_remote(job.package_path)


def test_uri_survives_a_queue_round_trip(sqs_queue):
    queue, _ = sqs_queue
    uri = "s3://ams-prod-objects/problems/p/v/package.cxxpkg"
    queue.publish(Job(job_id="j1", package_path=uri, submission_path=uri))
    lease = queue.claim()
    assert lease is not None
    assert lease.job.package_path == uri


def test_the_secondary_queue_is_drained_only_when_the_primary_is_empty(sqs_queue):
    """The rejudge queue exists so a bulk re-run cannot starve live
    submissions during a contest. But nothing consumed it, so rejudged
    submissions sat in it for ever — a queue nobody reads is worse than no
    queue at all.
    """
    _, client = sqs_queue
    secondary_url = client.create_queue(QueueName="test-rejudge")["QueueUrl"]
    primary_url = client.create_queue(QueueName="test-primary")["QueueUrl"]

    queue = SqsJobQueue(
        primary_url, wait_time_seconds=0, client=client, secondary_queue_url=secondary_url
    )

    client.send_message(QueueUrl=secondary_url, MessageBody=make_job("rejudged").model_dump_json())
    client.send_message(QueueUrl=primary_url, MessageBody=make_job("live").model_dump_json())

    first = queue.claim()
    assert first is not None
    assert first.job.job_id == "live", "live work must come first"

    queue.complete(first)

    second = queue.claim()
    assert second is not None
    assert second.job.job_id == "rejudged", "the backlog is drained once nothing is urgent"


def test_a_lease_is_completed_against_the_queue_it_came_from(sqs_queue):
    """Deleting from the wrong queue silently succeeds and leaves the message
    in place, so the job is redelivered until it dead-letters."""
    _, client = sqs_queue
    secondary_url = client.create_queue(QueueName="test-rejudge-2")["QueueUrl"]
    primary_url = client.create_queue(QueueName="test-primary-2")["QueueUrl"]

    queue = SqsJobQueue(
        primary_url, wait_time_seconds=0, client=client, secondary_queue_url=secondary_url
    )
    client.send_message(QueueUrl=secondary_url, MessageBody=make_job("r1").model_dump_json())

    lease = queue.claim()
    assert lease is not None
    assert lease.queue_url == secondary_url

    queue.complete(lease)
    assert queue.claim() is None, "the message must actually be gone"
