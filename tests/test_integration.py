"""End-to-end against the real cxxprobe binary.

Skipped unless CXXPROBE_BINARY points at a build. Everything else in the
suite uses a scripted stand-in, which proves the worker honours cxxprobe's
*documented* contract; this proves the real binary actually implements it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cxxprobe_worker.config import JudgeConfig
from cxxprobe_worker.executor import JobExecutor
from cxxprobe_worker.jobs import Job, JobStatus
from cxxprobe_worker.storage import FilesystemArtifactStorage
from cxxprobe_worker.workspace import WorkspaceManager

CXXPROBE = os.environ.get("CXXPROBE_BINARY") or shutil.which("cxxprobe")

pytestmark = pytest.mark.skipif(
    not CXXPROBE, reason="set CXXPROBE_BINARY to a cxxprobe build to run these"
)

CORRECT = '#include <iostream>\nint main(){int a,b;std::cin>>a>>b;std::cout<<(a+b)<<"\\n";}\n'
WRONG = '#include <iostream>\nint main(){int a,b;std::cin>>a>>b;std::cout<<(a+b+1)<<"\\n";}\n'


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture
def real_problem(tmp_path: Path) -> Path:
    """Scaffolds a v2 package with the real CLI, then fills it in.

    Skips rather than fails when CXXPROBE points at a build predating the v2
    package format — an old binary on PATH is a stale environment, not a
    worker bug.
    """
    assert CXXPROBE
    created = _run([CXXPROBE, "new", "contest", "Worker IT"], tmp_path)
    if created.returncode != 0:
        pytest.skip(f"cxxprobe new contest failed: {created.stderr or created.stdout}")
    contest = tmp_path / "worker-it"

    scaffolded = _run([CXXPROBE, "package", "init", "Sum Two"], contest)
    if scaffolded.returncode != 0:
        pytest.skip(
            "cxxprobe at "
            f"{CXXPROBE} has no `package init` — needs a build with the v2 package format"
        )
    problem = contest / "sum-two"
    (problem / "solutions" / "main.cpp").write_text(CORRECT)
    (problem / "tests" / "1.in").write_text("3 4\n")
    (problem / "tests" / "1.ans").write_text("7\n")
    # The scaffolded behavior checker needs GTest at judge time; this test is
    # about the worker's plumbing, so keep the problem to manual tests only.
    shutil.rmtree(problem / "checker", ignore_errors=True)
    return problem


def build_executor(tmp_path: Path, logger) -> JobExecutor:
    assert CXXPROBE
    return JobExecutor(
        JudgeConfig(binary=CXXPROBE, timeout_seconds=120.0),
        WorkspaceManager(tmp_path / "ws"),
        FilesystemArtifactStorage(tmp_path / "art"),
        logger,
    )


def _skip_without_sandbox(result) -> None:
    """Skip when the host has no usable cgroup delegation.

    The worker reports that as RETRYABLE (correctly — it's a property of the
    machine), which is indistinguishable here from a genuine infrastructure
    problem, so there is nothing meaningful left to assert.
    """
    if result.status is JobStatus.RETRYABLE and "cgroup" in (result.error or ""):
        pytest.skip("sandbox unavailable in this environment (no cgroup delegation)")


def test_correct_solution_is_judged_and_stored(real_problem: Path, tmp_path: Path, logger):
    submission = tmp_path / "sub.cpp"
    submission.write_text(CORRECT)
    executor = build_executor(tmp_path, logger)

    result = executor.execute(
        Job(job_id="it-1", package_path=real_problem, submission_path=submission)
    )
    _skip_without_sandbox(result)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert result.report is not None
    assert result.report["slug"] == "sum-two"
    assert result.report["overall"] == "PASS"
    assert "report.json" in result.artifacts


def test_wrong_solution_is_still_a_successful_job(real_problem: Path, tmp_path: Path, logger):
    submission = tmp_path / "sub.cpp"
    submission.write_text(WRONG)
    executor = build_executor(tmp_path, logger)

    result = executor.execute(
        Job(job_id="it-2", package_path=real_problem, submission_path=submission)
    )
    _skip_without_sandbox(result)

    # exit 1 from cxxprobe — a real WA — must not be mistaken for a job failure.
    assert result.status is JobStatus.SUCCEEDED, result.error
    assert result.report is not None
    assert result.report["overall"] == "FAIL"
    assert result.should_retry is False


def test_packaged_zip_is_judged(real_problem: Path, tmp_path: Path, logger):
    assert CXXPROBE
    contest = real_problem.parent
    packed = _run([CXXPROBE, "package", "pack", "--problems", "sum-two", "-o", "p.zip"], contest)
    if packed.returncode != 0:
        pytest.skip(f"cxxprobe package pack failed: {packed.stderr or packed.stdout}")
    submission = tmp_path / "sub.cpp"
    submission.write_text(CORRECT)
    executor = build_executor(tmp_path, logger)

    result = executor.execute(
        Job(job_id="it-3", package_path=contest / "p.zip", submission_path=submission)
    )
    _skip_without_sandbox(result)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert result.report is not None
    assert result.report["slug"] == "sum-two"
