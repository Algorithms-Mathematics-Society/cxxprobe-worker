"""The job model and its outcome.

A job is fully described by "which package, which submission" — the worker
holds no opinion about problems, verdicts, or contests. Everything it knows
about judging is that ``cxxprobe judge`` takes those two things and produces
a JSON report.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class JobError(RuntimeError):
    """Raised when a job payload is malformed."""


class JobStatus(StrEnum):
    SUCCEEDED = "succeeded"
    """cxxprobe ran and produced a report. Says nothing about the verdict —
    a WA submission is a *successful* job."""

    FAILED = "failed"
    """cxxprobe could not produce a report, and retrying won't help
    (bad package, missing submission, malformed job)."""

    RETRYABLE = "retryable"
    """The job did not complete for a reason that may not recur — a timeout,
    a missing binary, an I/O error. The queue should hand it back out."""


class Job(BaseModel):
    """One unit of work.

    ``package_path`` and ``submission_path`` are resolved by the worker
    against wherever the queue put them; the worker copies both into a
    private workspace before judging so a job can never mutate shared state.
    """

    model_config = {"extra": "forbid"}

    job_id: str = Field(min_length=1)
    package_path: Path
    """A cxxprobe pack zip, or an already-unpacked problem directory."""

    submission_path: Path
    """The source file to grade."""

    problem_slug: str | None = None
    """Advisory only — cxxprobe resolves the problem from the package itself.

    Carried through into logs and the result so an operator can correlate a
    job with whatever system enqueued it.
    """

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Opaque passthrough (submission id, contest id, …). Never interpreted."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise JobError(f"malformed job payload: {exc}") from exc


class JobResult(BaseModel):
    """What happened when a job ran."""

    model_config = {"extra": "forbid"}

    job_id: str
    status: JobStatus
    exit_code: int | None = None
    duration_seconds: float = 0.0
    report: dict[str, Any] | None = None
    """The parsed `cxxprobe judge` report, when one was produced."""

    error: str | None = None
    """Human-readable reason, set for FAILED and RETRYABLE."""

    artifacts: dict[str, str] = Field(default_factory=dict)
    """Artifact name → storage locator."""

    @property
    def ok(self) -> bool:
        return self.status is JobStatus.SUCCEEDED

    @property
    def should_retry(self) -> bool:
        return self.status is JobStatus.RETRYABLE
