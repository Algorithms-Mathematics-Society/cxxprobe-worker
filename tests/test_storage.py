from __future__ import annotations

from pathlib import Path

import pytest

from cxxprobe_worker.storage import (
    FilesystemArtifactStorage,
    IArtifactStorage,
    StorageError,
    build_storage,
)


def test_put_and_get_text_round_trips(storage: FilesystemArtifactStorage):
    storage.put_text("job-1", "report.json", '{"ok": true}')
    assert storage.get_bytes("job-1", "report.json") == b'{"ok": true}'


def test_put_file_copies_contents(storage: FilesystemArtifactStorage, tmp_path: Path):
    src = tmp_path / "sub.cpp"
    src.write_text("int main(){}\n")
    storage.put_file("job-1", "submission.cpp", src)
    assert storage.get_bytes("job-1", "submission.cpp") == b"int main(){}\n"


def test_exists_reflects_stored_state(storage: FilesystemArtifactStorage):
    assert storage.exists("job-1", "a.txt") is False
    storage.put_text("job-1", "a.txt", "x")
    assert storage.exists("job-1", "a.txt") is True


def test_list_artifacts_is_sorted_and_scoped_per_job(storage: FilesystemArtifactStorage):
    storage.put_text("job-1", "b.txt", "b")
    storage.put_text("job-1", "a.txt", "a")
    storage.put_text("job-2", "c.txt", "c")
    assert storage.list_artifacts("job-1") == ["a.txt", "b.txt"]
    assert storage.list_artifacts("job-2") == ["c.txt"]


def test_list_artifacts_of_unknown_job_is_empty(storage: FilesystemArtifactStorage):
    assert storage.list_artifacts("never-existed") == []


def test_nested_names_are_supported(storage: FilesystemArtifactStorage):
    storage.put_text("job-1", "logs/stderr.log", "boom")
    assert storage.list_artifacts("job-1") == ["logs/stderr.log"]


@pytest.mark.parametrize("name", ["../escape.txt", "a/../../escape.txt", "/etc/passwd", "", ".."])
def test_traversal_and_absolute_names_are_rejected(storage: FilesystemArtifactStorage, name: str):
    # Job ids and artifact names can both come from a queue message, so
    # neither is trusted — a job must not be able to write outside its own
    # directory.
    with pytest.raises(StorageError):
        storage.put_text("job-1", name, "x")


def test_traversal_in_job_id_is_rejected(storage: FilesystemArtifactStorage):
    with pytest.raises(StorageError):
        storage.put_text("../elsewhere", "a.txt", "x")


def test_reading_a_missing_artifact_raises(storage: FilesystemArtifactStorage):
    with pytest.raises(StorageError):
        storage.get_bytes("job-1", "nope.txt")


def test_build_storage_returns_filesystem_backend(tmp_path: Path):
    built = build_storage("filesystem", tmp_path)
    assert isinstance(built, FilesystemArtifactStorage)
    assert isinstance(built, IArtifactStorage)


def test_build_storage_rejects_unknown_backend(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown storage backend"):
        build_storage("s3", tmp_path)
