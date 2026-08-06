"""SQS implementation of ``IJobQueue``.

The local queue was deliberately modelled on SQS's lease semantics, so this
is a thin mapping rather than an adaptation:

    claim()    → receive_message (long-poll), Lease.receipt = ReceiptHandle
    complete() → delete_message
    release()  → change_message_visibility(VisibilityTimeout=0)

Two things move *out* of the worker and into SQS here:

* **Dead-lettering** — SQS's redrive policy (`maxReceiveCount`) handles it
  natively, so the local queue's attempt-counting filename trick is gone.
* **Delivery counting** — `ApproximateReceiveCount` comes back on the
  message, so the worker reads it rather than tracking it.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

from cxxprobe_worker.jobs import Job, JobError
from cxxprobe_worker.queue import Lease, QueueError

if TYPE_CHECKING:  # pragma: no cover
    from mypy_boto3_sqs.client import SQSClient


# Retry pacing for released jobs. A job that fails in under a second would
# otherwise be redelivered several times a second until the DLQ catches it.
RELEASE_BACKOFF_BASE_SECONDS = 2
RELEASE_BACKOFF_MAX_SECONDS = 300


class SqsJobQueue:
    """A job queue backed by an SQS standard queue."""

    def __init__(
        self,
        queue_url: str,
        region: str | None = None,
        wait_time_seconds: int = 20,
        client: Any = None,
        secondary_queue_url: str = "",
    ) -> None:
        self._queue_url = queue_url
        # Polled only when the primary is empty. The rejudge queue is
        # separate precisely so a bulk re-run cannot starve live submissions
        # during a contest — but a queue nobody consumes is worse than no
        # queue at all, and rejudged submissions simply sat there for ever.
        self._secondary_queue_url = secondary_queue_url.strip()
        # Long-polling: 20s is SQS's maximum and the difference between one
        # API call per job and one every poll interval.
        self._wait_time = wait_time_seconds
        if client is not None:
            self._sqs: SQSClient = client
        else:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover
                raise QueueError(
                    "the sqs queue backend needs boto3 — install with `uv sync --extra aws`"
                ) from exc
            self._sqs = boto3.client("sqs", region_name=region)

    @property
    def queue_url(self) -> str:
        return self._queue_url

    def publish(self, job: Job) -> None:
        try:
            self._sqs.send_message(
                QueueUrl=self._queue_url,
                MessageBody=job.model_dump_json(),
            )
        except Exception as exc:
            raise QueueError(f"cannot publish job {job.job_id}: {exc}") from exc

    def claim(self) -> Lease | None:
        lease = self._claim_from(self._queue_url)
        if lease is not None or not self._secondary_queue_url:
            return lease
        # Nothing urgent waiting, so catch up on the backlog.
        return self._claim_from(self._secondary_queue_url)

    def _claim_from(self, queue_url: str) -> Lease | None:
        try:
            resp = self._sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=self._wait_time,
                AttributeNames=["ApproximateReceiveCount"],
            )
        except Exception as exc:
            raise QueueError(f"cannot receive from {queue_url}: {exc}") from exc

        messages = resp.get("Messages", [])
        if not messages:
            return None
        message = messages[0]
        receipt = message["ReceiptHandle"]

        try:
            job = Job.from_dict(json.loads(message["Body"]))
        except (json.JSONDecodeError, JobError):
            # Unparseable message: delete it so it stops being redelivered.
            # SQS's DLQ only catches repeated *receives*, not poison bodies
            # that fail before the worker ever runs them.
            # Best effort: if the delete fails, SQS's redrive policy is the
            # backstop that eventually dead-letters it.
            with contextlib.suppress(Exception):
                self._sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
            return None

        attempt = int(message.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
        # The queue is carried on the lease: completing a rejudge against the
        # primary queue would silently fail to delete it, and the job would
        # be redelivered for ever.
        return Lease(job=job, receipt=receipt, delivery_attempt=attempt, queue_url=queue_url)

    def complete(self, lease: Lease) -> None:
        try:
            self._sqs.delete_message(
                QueueUrl=lease.queue_url or self._queue_url, ReceiptHandle=lease.receipt
            )
        except Exception as exc:
            raise QueueError(f"cannot complete job {lease.job.job_id}: {exc}") from exc

    def release(self, lease: Lease) -> None:
        """Return a job to the queue, backing off as attempts accumulate.

        Releasing at zero visibility redelivers the message *immediately*.
        For a job that fails fast — an unreadable package, say — that is a hot
        loop: the worker re-claims, fails in under a second, releases, and
        repeats several times a second until the redrive policy finally
        dead-letters it. It burns a worker and floods SQS with requests while
        real submissions wait behind it.

        Backing off exponentially keeps the retry (the machine really might
        be the problem) without the spin.
        """
        try:
            self._sqs.change_message_visibility(
                QueueUrl=lease.queue_url or self._queue_url,
                ReceiptHandle=lease.receipt,
                VisibilityTimeout=self._backoff_seconds(lease.delivery_attempt),
            )
        except Exception as exc:
            raise QueueError(f"cannot release job {lease.job.job_id}: {exc}") from exc

    def _backoff_seconds(self, attempt: int) -> int:
        """1st retry ~2s, then 4s, 8s… capped.

        Capped well under the queue's visibility timeout so a released job is
        never invisible for longer than a running one would be.
        """
        delay = RELEASE_BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1))
        return int(min(delay, RELEASE_BACKOFF_MAX_SECONDS))

    def depth(self) -> int:
        try:
            resp = self._sqs.get_queue_attributes(
                QueueUrl=self._queue_url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )
        except Exception as exc:
            raise QueueError(f"cannot read queue depth: {exc}") from exc
        return int(resp["Attributes"].get("ApproximateNumberOfMessages", "0"))
