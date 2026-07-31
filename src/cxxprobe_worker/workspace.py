"""Per-job scratch directories.

Every job gets its own directory, and it is removed when the job ends —
success or failure — because a judging host that accumulates workspaces
eventually fails every job with ENOSPC, which is a far more confusing
outage than a lost repro.

``keep_on_failure`` opts back into retention for environments where the disk
is disposable and the repro is worth more.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(RuntimeError):
    """Raised when a workspace cannot be created."""


@dataclass(frozen=True)
class Workspace:
    """A job's private directory tree."""

    job_id: str
    root: Path

    @property
    def package_dir(self) -> Path:
        """Where the problem package is unpacked."""
        return self.root / "package"

    @property
    def submission_path(self) -> Path:
        """The source file handed to `cxxprobe judge --submission`."""
        return self.root / "submission.cpp"

    @property
    def report_path(self) -> Path:
        """Where `cxxprobe judge --output` writes its JSON report."""
        return self.root / "report.json"


class WorkspaceManager:
    """Creates and destroys per-job workspaces under a shared root."""

    def __init__(self, root: Path, keep_on_failure: bool = False) -> None:
        self._root = Path(root)
        self._keep_on_failure = keep_on_failure

    @property
    def root(self) -> Path:
        return self._root

    def create(self, job_id: str) -> Workspace:
        # The uuid suffix keeps a retried job from colliding with the
        # leftovers of its own previous attempt.
        name = f"{job_id}-{uuid.uuid4().hex[:8]}"
        path = self._root / name
        try:
            path.mkdir(parents=True, exist_ok=False)
            (path / "package").mkdir()
        except OSError as exc:
            raise WorkspaceError(f"cannot create workspace for job {job_id}: {exc}") from exc
        return Workspace(job_id=job_id, root=path)

    def destroy(self, workspace: Workspace) -> None:
        shutil.rmtree(workspace.root, ignore_errors=True)

    @contextmanager
    def session(self, job_id: str) -> Iterator[Workspace]:
        """Create a workspace, yield it, and clean it up unconditionally.

        On an exception the workspace is retained if ``keep_on_failure`` is
        set, so the failure can be reproduced by hand; otherwise it goes away
        like any other.
        """
        workspace = self.create(job_id)
        try:
            yield workspace
        except BaseException:
            if not self._keep_on_failure:
                self.destroy(workspace)
            raise
        else:
            self.destroy(workspace)

    def prune(self) -> int:
        """Remove every workspace under the root. Returns how many it deleted.

        For startup recovery: a worker killed mid-job leaves its workspace
        behind, and nothing else will ever clean it up.
        """
        if not self._root.is_dir():
            return 0
        removed = 0
        for child in self._root.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        return removed
