"""Worker configuration: a YAML file per environment, validated on load.

The worker runs in three environments — ``test``, ``staging``, ``prod`` — that
differ in concurrency, paths, and log verbosity but not in structure. Each
gets a file under ``config/``; ``load_config`` picks one by name.

Every field can be overridden from the environment with a ``CXXPROBE_WORKER_``
prefix and ``__`` as the nesting separator (``CXXPROBE_WORKER_JUDGE__TIMEOUT_SECONDS=120``),
which is how a container deployment tunes a baked-in image without rebuilding it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

ENV_PREFIX = "CXXPROBE_WORKER_"
ENV_NESTED_SEP = "__"

Environment = Literal["test", "staging", "prod"]


class ConfigError(RuntimeError):
    """Raised when a config file is missing, unparseable, or fails validation."""


class JudgeConfig(BaseModel):
    """How to invoke the cxxprobe binary that does the actual judging."""

    model_config = {"extra": "forbid"}

    binary: str = "cxxprobe"
    """Resolved via PATH unless it contains a separator."""

    timeout_seconds: float = Field(default=300.0, gt=0)
    """Wall-clock ceiling on one `cxxprobe judge` invocation.

    This is a backstop around cxxprobe's own per-case sandbox limits, not a
    replacement for them: it catches a wedged compile or a cxxprobe bug, not
    a slow submission.
    """

    extra_args: list[str] = Field(default_factory=list)


class WorkspaceConfig(BaseModel):
    """Where per-job scratch directories are created."""

    model_config = {"extra": "forbid"}

    root: Path = Path("/tmp/cxxprobe-worker/workspaces")
    keep_on_failure: bool = False
    """Retain a failed job's workspace for debugging.

    Off by default even in staging — a wedged worker filling its disk with
    failed workspaces is a worse failure than losing a repro.
    """


class StorageConfig(BaseModel):
    """Where job artifacts (reports, logs, submissions) are persisted."""

    model_config = {"extra": "forbid"}

    backend: Literal["filesystem", "s3"] = "filesystem"

    root: Path = Path("/var/lib/cxxprobe-worker/artifacts")
    """Filesystem backend only."""

    bucket: str = ""
    """S3 backend only. Required when backend is ``s3``."""

    prefix: str = "artifacts"
    """S3 key prefix, so artifacts share a bucket with packages and sources."""


class QueueConfig(BaseModel):
    """Where jobs come from."""

    model_config = {"extra": "forbid"}

    backend: Literal["local", "sqs"] = "local"

    root: Path = Path("/var/lib/cxxprobe-worker/queue")
    """Local backend only."""

    queue_url: str = ""
    # Polled only when `queue_url` is empty, so a backlog never delays live
    # work. This is where rejudges land.
    secondary_queue_url: str = ""
    """SQS backend only. Required when backend is ``sqs``."""

    wait_time_seconds: int = Field(default=20, ge=0, le=20)
    """SQS long-poll duration. 20s is the maximum and the difference between
    one API call per job and one per poll interval."""
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    visibility_timeout_seconds: float = Field(default=600.0, gt=0)
    """How long a claimed job may run before another worker may reclaim it."""

    max_delivery_attempts: int = Field(default=5, ge=1)
    """Deliveries before a job is parked in ``dead/``.

    A retryable classification can't distinguish "this host is broken" from
    "this package is broken" on the first attempt, so a cap is what stops a
    permanently-bad job from recirculating for ever.
    """


class AwsConfig(BaseModel):
    """Shared AWS settings for whichever backends are enabled."""

    model_config = {"extra": "forbid"}

    region: str = "ap-south-1"


class ControlPlaneConfig(BaseModel):
    """ams-api, where the worker registers and posts results.

    Empty ``base_url`` means standalone mode: judge and store artifacts, but
    report to nobody. That is what keeps the local/filesystem path usable
    without any AMS deployment at all.
    """

    model_config = {"extra": "forbid"}

    base_url: str = ""
    api_key: str = ""
    pool: str = "default"
    heartbeat_seconds: float = Field(default=10.0, gt=0)
    timeout_seconds: float = Field(default=30.0, gt=0)


class MonitoringConfig(BaseModel):
    model_config = {"extra": "forbid"}

    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["json", "text"] = "json"
    health_file: Path | None = None
    """If set, a heartbeat JSON file rewritten each poll.

    Deliberately a file rather than an HTTP endpoint: the worker has no
    inbound network surface, and a file is enough for a container liveness
    probe (`test -f` plus an mtime check).
    """


class WorkerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    environment: Environment = "test"
    worker_id: str = "worker-1"
    concurrency: int = Field(default=1, ge=1)
    """Jobs run in parallel.

    Each judged submission is already sandboxed and resource-capped by
    cxxprobe itself, so this is about saturating the host, not isolation.
    """

    max_jobs: int | None = Field(default=None, ge=1)
    """Exit cleanly after this many jobs. ``None`` means run forever.

    Used by ``--once`` and by deployments that prefer periodic recycling over
    trusting a long-lived process not to leak.
    """

    aws: AwsConfig = Field(default_factory=AwsConfig)
    control_plane: ControlPlaneConfig = Field(default_factory=ControlPlaneConfig)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)

    @field_validator("worker_id")
    @classmethod
    def _worker_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("worker_id must not be blank")
        return v


def _coerce_scalar(raw: str) -> Any:
    """Parse an env-var string the same way PyYAML would parse the literal.

    Keeps ``CXXPROBE_WORKER_CONCURRENCY=4`` an int and
    ``...KEEP_ON_FAILURE=true`` a bool, so overrides don't have to be
    stringly-typed at every call site.

    The empty string is the one place we deliberately disagree with YAML.
    ``yaml.safe_load("")`` is ``None``, but ``FOO=`` in an environment means
    "empty", not "null" — and every field it is plausibly used on is a
    ``str`` whose empty value *means* something: an empty
    ``control_plane.base_url`` is standalone mode, an empty
    ``secondary_queue_url`` is "don't poll a second queue". Parsing those as
    ``None`` failed validation and took the worker down at startup, which is
    a rotten way to discover you cannot turn a setting off.
    """
    if raw == "":
        return ""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    merged = dict(data)
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key.removeprefix(ENV_PREFIX).lower().split(ENV_NESTED_SEP)
        cursor = merged
        for part in path[:-1]:
            existing = cursor.get(part)
            branch: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
            cursor[part] = branch
            cursor = branch
        cursor[path[-1]] = _coerce_scalar(raw)
    return merged


def config_path_for(environment: str, config_dir: Path) -> Path:
    return config_dir / f"{environment}.yaml"


def load_config(
    environment: str = "test",
    config_dir: Path | None = None,
    environ: dict[str, str] | None = None,
) -> WorkerConfig:
    """Load ``<config_dir>/<environment>.yaml`` and apply env overrides.

    Raises ConfigError — never a bare pydantic/yaml error — so callers have
    one exception type to catch and one message shape to print.
    """
    config_dir = config_dir or Path("config")
    # os.environ is a Mapping, not a dict — copy it so the rest of this
    # function has one concrete type to work with.
    effective_env: dict[str, str] = dict(os.environ) if environ is None else environ

    path = config_path_for(environment, config_dir)
    if not path.exists():
        raise ConfigError(f"no config for environment '{environment}': {path} does not exist")

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(raw).__name__}")

    raw.setdefault("environment", environment)
    merged = _apply_env_overrides(raw, effective_env)

    try:
        return WorkerConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
