"""The job model and its outcome.

A job is fully described by "which package, which submission" — the worker
holds no opinion about problems, verdicts, or contests. Everything it knows
about judging is that ``cxxprobe judge`` takes those two things and produces
a JSON report.

A *project* job is the one exception to "one submission": a team round's
parts are written by different people and only prove anything once linked
together, so it carries one submission per part label instead.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# A part label becomes a filename in the job's workspace, so it is held to a
# shape that can never name anything outside it: no separators, no dot-dot.
PART_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


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

    # Deliberately `str`, not `Path`: these may be `s3://bucket/key` URIs,
    # and pathlib collapses `//` to `/`, which silently corrupts a URI into
    # something that no longer parses as one.
    package_path: str
    """A cxxprobe pack zip, an unpacked problem directory, or an s3:// URI."""

    job_type: Literal["problem", "project", "benchmark"] = "problem"
    """Which shape this job is. Explicit rather than inferred from which
    fields happen to be set, so a malformed payload fails at parse time
    instead of halfway through judging.

    Defaults to ``problem`` so every payload already in flight, none of which
    carries the field, keeps meaning exactly what it meant.
    """

    submission_path: str | None = None
    """The source file to grade — local path or s3:// URI.

    Required for a ``problem`` job and refused on a ``project`` job, whose
    sources arrive in ``parts``. Never a joined list of several.
    """

    parts: dict[str, str] | None = None
    """Part label → submission URI, for a ``project`` job only."""

    project_slug: str | None = None
    """The project inside the package to judge. Required for a ``project``
    job: unlike a problem package, a contest pack can hold several."""

    fallback: dict[str, Literal["reference", "empty"]] | None = None
    """Part label → what cxxprobe substitutes when that part is not supplied.

    ``reference`` keeps the other parts gradeable when one never showed up;
    ``empty`` proves the integration genuinely needs it. Project jobs only.
    A label named here may be left out of ``parts``.
    """

    team_id: str | None = None
    """Benchmark jobs: the team whose revision this is. Supersession and the
    per-team rate limit are keyed on it."""

    revision: str | None = None
    """Benchmark jobs: the source revision hash, recorded with the result so
    a number is traceable to the code that produced it."""

    revision_seq: int | None = Field(default=None, ge=0)
    """Benchmark jobs: increases with every revision a team submits. A job
    older than the newest one benchmarked for its team is never scored."""

    benchmark_input: str | None = None
    """Benchmark jobs: the hidden benchmark input, local path or s3:// URI.
    Regenerated per round by whatever enqueues the job; the worker records
    its content hash with the result."""

    problem_slug: str | None = None
    """Advisory only — cxxprobe resolves the problem from the package itself.

    Carried through into logs and the result so an operator can correlate a
    job with whatever system enqueued it.
    """

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Passthrough plus optional attempt_token and reserved delivery marker."""

    @field_validator("metadata")
    @classmethod
    def _validate_attempt_token(cls, value: dict[str, Any]) -> dict[str, Any]:
        token = value.get("attempt_token")
        if token is not None:
            if not isinstance(token, str):
                raise ValueError("metadata.attempt_token must be a UUID string or null")
            value = {**value, "attempt_token": str(UUID(token))}
        return value

    @property
    def attempt_token(self) -> str | None:
        token = self.metadata.get("attempt_token")
        return token if isinstance(token, str) else None

    custom_cases: list[dict[str, Any]] | None = None
    """Candidate-supplied cases: ``[{"label":…, "input":…, "expected":…}]``.

    When set, the job runs ONLY these and the problem's own tests are never
    opened, so a hidden input or answer cannot reach the result. ``expected``
    is optional per case: without it the case is run but not judged, which is
    the "just show me what it prints" path.

    ``None`` and ``[]`` are deliberately different. ``None`` is an ordinary
    judging job; ``[]`` would be a custom run with nothing to run, which the
    executor refuses rather than silently judging the real tests.
    """

    @field_validator("package_path", "submission_path", mode="before")
    @classmethod
    def _coerce_location(cls, v: Any) -> str | None:
        """Accept a Path or a string, always store a string.

        Callers holding a local `Path` shouldn't have to stringify, but the
        stored value must stay a string: `Path("s3://b/k")` collapses the
        double slash to `s3:/b/k`, which no longer parses as a URI.
        """
        return None if v is None else str(v)

    @field_validator("parts", mode="before")
    @classmethod
    def _coerce_part_locations(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {k: str(loc) for k, loc in v.items()}
        return v

    @model_validator(mode="after")
    def _shape_matches_type(self) -> Job:
        """Each job type carries exactly the fields it needs, and no others."""
        if self.job_type == "problem":
            if self.parts is not None:
                raise ValueError("a problem job must not carry parts")
            if self.project_slug is not None:
                raise ValueError("a problem job must not carry project_slug")
            if self.fallback is not None:
                raise ValueError("a problem job must not carry fallback")
            for name in ("team_id", "revision", "revision_seq", "benchmark_input"):
                if getattr(self, name) is not None:
                    raise ValueError(f"only a benchmark job carries {name}")
            if not self.submission_path:
                raise ValueError("a problem job needs submission_path")
            return self

        bench_only = {
            "team_id": self.team_id,
            "revision": self.revision,
            "revision_seq": self.revision_seq,
            "benchmark_input": self.benchmark_input,
        }
        if self.job_type == "benchmark":
            missing = [name for name, value in bench_only.items() if value in (None, "")]
            if missing:
                raise ValueError(f"a benchmark job needs {', '.join(missing)}")
            if self.fallback:
                raise ValueError(
                    "a benchmark job times every part as submitted; it takes no fallback"
                )
            if not PART_LABEL.fullmatch(self.team_id or ""):
                raise ValueError(f"team_id {self.team_id!r} is not a plain name")
        else:
            present = [name for name, value in bench_only.items() if value is not None]
            if present:
                raise ValueError(f"only a benchmark job carries {', '.join(present)}")

        if self.submission_path is not None:
            raise ValueError("a project job takes its sources from parts, not submission_path")
        if not self.parts:
            raise ValueError("a project job needs at least one entry in parts")
        if not self.project_slug:
            raise ValueError("a project job needs project_slug")
        if self.custom_cases is not None:
            raise ValueError("custom_cases is not supported on a project job")
        for label in [*self.parts, *(self.fallback or {})]:
            if not PART_LABEL.fullmatch(label):
                raise ValueError(f"part label {label!r} is not a plain name")
        return self

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
    attempt_token: str | None = None
    exit_code: int | None = None
    duration_seconds: float = 0.0
    report: dict[str, Any] | None = None
    """The parsed `cxxprobe judge` report, when one was produced."""

    error: str | None = None
    """Human-readable reason, set for FAILED and RETRYABLE."""

    artifacts: dict[str, str] = Field(default_factory=dict)
    """Artifact name → storage locator."""

    judge_signal: str | None = None
    """The signal that killed cxxprobe itself (``SIGPIPE``, ``SIGSEGV``, …),
    when that is how the run ended. Set only when the submission is to blame,
    which makes it a runtime error with a verdict rather than a retry."""

    provenance: dict[str, Any] | None = None
    """Benchmark jobs: machine_id, calibration_ok, input_hash, revision_hash,
    plus team_id and revision_seq, published beside the engine's record."""

    package_cache: Literal["hit", "miss"] | None = None
    """Whether this job's package came from the local cache. ``None`` when
    the package was local and the cache never applied."""

    @property
    def ok(self) -> bool:
        return self.status is JobStatus.SUCCEEDED

    @property
    def should_retry(self) -> bool:
        return self.status is JobStatus.RETRYABLE
