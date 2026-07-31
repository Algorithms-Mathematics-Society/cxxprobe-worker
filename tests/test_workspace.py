from __future__ import annotations

from pathlib import Path

import pytest

from cxxprobe_worker.workspace import WorkspaceManager


def test_create_makes_an_isolated_directory(workspaces: WorkspaceManager):
    a = workspaces.create("job-1")
    b = workspaces.create("job-1")
    # Same job id, different directories — a retry must not land in the
    # leftovers of its own previous attempt.
    assert a.root != b.root
    assert a.root.is_dir()
    assert a.package_dir.is_dir()


def test_session_cleans_up_on_success(workspaces: WorkspaceManager):
    with workspaces.session("job-1") as ws:
        root = ws.root
        assert root.is_dir()
    assert not root.exists()


def test_session_cleans_up_on_failure_by_default(workspaces: WorkspaceManager):
    with pytest.raises(RuntimeError), workspaces.session("job-1") as ws:
        root = ws.root
        raise RuntimeError("boom")
    assert not root.exists()


def test_session_retains_on_failure_when_configured(tmp_path: Path):
    manager = WorkspaceManager(tmp_path / "ws", keep_on_failure=True)
    with pytest.raises(RuntimeError), manager.session("job-1") as ws:
        root = ws.root
        raise RuntimeError("boom")
    assert root.is_dir()


def test_prune_removes_leftover_workspaces(workspaces: WorkspaceManager):
    workspaces.create("job-1")
    workspaces.create("job-2")
    assert workspaces.prune() == 2
    assert workspaces.prune() == 0


def test_prune_on_missing_root_is_a_noop(tmp_path: Path):
    assert WorkspaceManager(tmp_path / "never-created").prune() == 0


def test_paths_are_under_the_workspace_root(workspaces: WorkspaceManager):
    ws = workspaces.create("job-1")
    assert ws.submission_path.parent == ws.root
    assert ws.report_path.parent == ws.root
    assert ws.package_dir.parent == ws.root
