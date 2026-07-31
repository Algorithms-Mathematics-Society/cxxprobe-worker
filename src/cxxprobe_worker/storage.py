"""Artifact storage: where a finished job's outputs are persisted.

A job produces small, durable artifacts — the judge report, the submission
that was graded, captured stderr. They outlive the workspace (which is
deleted the moment the job ends) because they're what someone reads when
asking "why did submission X get WA?" hours later.

``IArtifactStorage`` is the seam an S3 backend would implement. Only the
filesystem backend exists today; the protocol exists so adding one later is
a new file rather than a refactor.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable


class StorageError(RuntimeError):
    """Raised when an artifact cannot be written or read."""


@runtime_checkable
class IArtifactStorage(Protocol):
    """Content-addressed-by-key blob store, scoped per job.

    Keys are ``<job_id>/<name>`` relative paths. Implementations must treat
    a key as opaque and must reject anything that would escape the job's own
    namespace.
    """

    def put_bytes(self, job_id: str, name: str, data: bytes) -> str:
        """Store ``data`` and return a backend-specific locator (path, URL, …)."""
        ...

    def put_text(self, job_id: str, name: str, text: str) -> str: ...

    def put_file(self, job_id: str, name: str, source: Path) -> str: ...

    def get_bytes(self, job_id: str, name: str) -> bytes: ...

    def exists(self, job_id: str, name: str) -> bool: ...

    def list_artifacts(self, job_id: str) -> list[str]:
        """Names stored under ``job_id``, sorted. Empty if the job is unknown."""
        ...


def _validate_name(name: str) -> None:
    """Reject any name that could write outside the job's own directory.

    A job id and artifact name can both originate from a queue message, so
    neither is trusted input.
    """
    if not name or name in {".", ".."}:
        raise StorageError(f"invalid artifact name: {name!r}")
    if name.startswith("/") or name.startswith("\\"):
        raise StorageError(f"artifact name must be relative: {name!r}")
    if ".." in Path(name).parts:
        raise StorageError(f"artifact name must not traverse upwards: {name!r}")


class FilesystemArtifactStorage:
    """Stores artifacts as plain files under ``root/<job_id>/<name>``.

    Deliberately boring: a human debugging a bad verdict can `cat` the
    report without any tooling, and a `du -sh` answers "how much are we
    storing".
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, job_id: str, name: str) -> Path:
        _validate_name(job_id)
        _validate_name(name)
        return self._root / job_id / name

    def put_bytes(self, job_id: str, name: str, data: bytes) -> str:
        path = self._path_for(job_id, name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            raise StorageError(f"cannot write artifact {job_id}/{name}: {exc}") from exc
        return str(path)

    def put_text(self, job_id: str, name: str, text: str) -> str:
        return self.put_bytes(job_id, name, text.encode())

    def put_file(self, job_id: str, name: str, source: Path) -> str:
        path = self._path_for(job_id, name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, path)
        except OSError as exc:
            raise StorageError(f"cannot copy {source} to {job_id}/{name}: {exc}") from exc
        return str(path)

    def get_bytes(self, job_id: str, name: str) -> bytes:
        path = self._path_for(job_id, name)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageError(f"cannot read artifact {job_id}/{name}: {exc}") from exc

    def exists(self, job_id: str, name: str) -> bool:
        return self._path_for(job_id, name).is_file()

    def list_artifacts(self, job_id: str) -> list[str]:
        _validate_name(job_id)
        base = self._root / job_id
        if not base.is_dir():
            return []
        return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())


def build_storage(backend: str, root: Path) -> IArtifactStorage:
    """Construct the configured backend.

    Raises ValueError rather than falling back, so a typo'd backend name is
    a startup failure instead of silently writing to the wrong place.
    """
    if backend == "filesystem":
        return FilesystemArtifactStorage(root)
    raise ValueError(f"unknown storage backend: {backend!r}")
