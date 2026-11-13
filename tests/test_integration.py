"""End-to-end against the real cxxprobe binary.

Skipped unless CXXPROBE_BINARY points at a build, or failed when
--require-real-engine is set. Everything else in the
suite uses a scripted stand-in, which proves the worker honours cxxprobe's
*documented* contract; this proves the real binary actually implements it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import NoReturn

import pytest

from cxxprobe_worker.config import JudgeConfig
from cxxprobe_worker.executor import JobExecutor
from cxxprobe_worker.jobs import Job, JobStatus
from cxxprobe_worker.storage import FilesystemArtifactStorage
from cxxprobe_worker.workspace import WorkspaceManager

CORRECT = '#include <iostream>\nint main(){int a,b;std::cin>>a>>b;std::cout<<(a+b)<<"\\n";}\n'
WRONG = '#include <iostream>\nint main(){int a,b;std::cin>>a>>b;std::cout<<(a+b+1)<<"\\n";}\n'


def _run(
    argv: list[str], cwd: Path, pytestconfig: pytest.Config
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _unavailable(pytestconfig, f"cxxprobe command could not run: {exc}")


def _unavailable(pytestconfig: pytest.Config, reason: str) -> NoReturn:
    if pytestconfig.getoption("--require-real-engine"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture
def real_problem(tmp_path: Path, real_engine_binary: str, pytestconfig: pytest.Config) -> Path:
    """Scaffolds a v2 package with the real CLI, then fills it in.

    Skips rather than fails when CXXPROBE points at a build predating the v2
    package format — an old binary on PATH is a stale environment, not a
    worker bug.
    """
    created = _run([real_engine_binary, "new", "contest", "Worker IT"], tmp_path, pytestconfig)
    if created.returncode != 0:
        _unavailable(
            pytestconfig, f"cxxprobe new contest failed: {created.stderr or created.stdout}"
        )
    contest = tmp_path / "worker-it"

    scaffolded = _run([real_engine_binary, "package", "init", "Sum Two"], contest, pytestconfig)
    if scaffolded.returncode != 0:
        _unavailable(
            pytestconfig,
            "cxxprobe package init failed (needs v2 package format): "
            f"{scaffolded.stderr or scaffolded.stdout}",
        )
    problem = contest / "sum-two"
    (problem / "solutions" / "main.cpp").write_text(CORRECT)
    (problem / "tests" / "1.in").write_text("3 4\n")
    (problem / "tests" / "1.ans").write_text("7\n")
    # The scaffolded behavior checker needs GTest at judge time; this test is
    # about the worker's plumbing, so keep the problem to manual tests only.
    shutil.rmtree(problem / "checker", ignore_errors=True)
    return problem


def build_executor(tmp_path: Path, logger, real_engine_binary: str) -> JobExecutor:
    return JobExecutor(
        JudgeConfig(binary=real_engine_binary, timeout_seconds=120.0),
        WorkspaceManager(tmp_path / "ws"),
        FilesystemArtifactStorage(tmp_path / "art"),
        logger,
    )


def _skip_without_sandbox(result, pytestconfig: pytest.Config) -> None:
    """Skip when the host has no usable cgroup delegation.

    The worker reports that as RETRYABLE (correctly — it's a property of the
    machine), which is indistinguishable here from a genuine infrastructure
    problem, so there is nothing meaningful left to assert.
    """
    if result.status is JobStatus.RETRYABLE and "cgroup" in (result.error or ""):
        _unavailable(pytestconfig, f"sandbox unavailable (no cgroup delegation): {result.error}")


def test_correct_solution_is_judged_and_stored(
    real_problem: Path, tmp_path: Path, logger, real_engine_binary: str, pytestconfig: pytest.Config
):
    submission = tmp_path / "sub.cpp"
    submission.write_text(CORRECT)
    executor = build_executor(tmp_path, logger, real_engine_binary)

    result = executor.execute(
        Job(job_id="it-1", package_path=str(real_problem), submission_path=str(submission))
    )
    _skip_without_sandbox(result, pytestconfig)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert result.report is not None
    assert result.report["slug"] == "sum-two"
    assert result.report["overall"] == "PASS"
    assert "report.json" in result.artifacts


def test_wrong_solution_is_still_a_successful_job(
    real_problem: Path, tmp_path: Path, logger, real_engine_binary: str, pytestconfig: pytest.Config
):
    submission = tmp_path / "sub.cpp"
    submission.write_text(WRONG)
    executor = build_executor(tmp_path, logger, real_engine_binary)

    result = executor.execute(
        Job(job_id="it-2", package_path=str(real_problem), submission_path=str(submission))
    )
    _skip_without_sandbox(result, pytestconfig)

    # exit 1 from cxxprobe — a real WA — must not be mistaken for a job failure.
    assert result.status is JobStatus.SUCCEEDED, result.error
    assert result.report is not None
    assert result.report["slug"] == "sum-two"
    assert result.report["overall"] == "FAIL"
    assert result.should_retry is False


def test_packaged_zip_is_judged(
    real_problem: Path, tmp_path: Path, logger, real_engine_binary: str, pytestconfig: pytest.Config
):
    contest = real_problem.parent
    packed = _run(
        [real_engine_binary, "package", "pack", "--problems", "sum-two", "-o", "p.zip"],
        contest,
        pytestconfig,
    )
    if packed.returncode != 0:
        _unavailable(
            pytestconfig, f"cxxprobe package pack failed: {packed.stderr or packed.stdout}"
        )
    submission = tmp_path / "sub.cpp"
    submission.write_text(CORRECT)
    executor = build_executor(tmp_path, logger, real_engine_binary)

    result = executor.execute(
        Job(job_id="it-3", package_path=str(contest / "p.zip"), submission_path=str(submission))
    )
    _skip_without_sandbox(result, pytestconfig)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert result.report is not None
    assert result.report["slug"] == "sum-two"


def test_multifile_bundle_is_judged(
    real_problem: Path, tmp_path: Path, logger, real_engine_binary: str, pytestconfig: pytest.Config
):
    # The candidate's changed header must be used while a declared reference
    # header remains available from the private staged package.
    attachments = real_problem / "attachments"
    attachments.mkdir(exist_ok=True)
    (attachments / "sum.hpp").write_text("#error stale starter header\n")
    (attachments / "offset.hpp").write_text("inline int offset(){return 0;}\n")
    spec_path = real_problem / "problem.yaml"
    spec = spec_path.read_text()
    if "attachments:\n  dir: attachments\n" in spec:
        spec = spec.replace(
            "attachments:\n  dir: attachments\n",
            "attachments:\n  dir: attachments\n  reference_files: [offset.hpp]\n",
        )
    else:
        spec += "\nattachments:\n  dir: attachments\n  reference_files: [offset.hpp]\n"
    spec_path.write_text(spec)
    bundle = tmp_path / "submission.bundle.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": "ams-source-bundle-v1",
                    "entrypoint": "main.cpp",
                    "compilation_units": ["main.cpp", "sum.cpp"],
                }
            ),
        )
        archive.writestr(
            "files/main.cpp",
            '#include <iostream>\n#include "sum.hpp"\n'
            'int main(){int a,b;std::cin>>a>>b;std::cout<<sum(a,b)<<"\\n";}\n',
        )
        archive.writestr("files/sum.hpp", "int sum(int a, int b);\n")
        archive.writestr(
            "files/sum.cpp",
            '#include "sum.hpp"\n#include "offset.hpp"\n'
            "int sum(int a,int b){return a+b+offset();}\n",
        )

    result = build_executor(tmp_path, logger, real_engine_binary).execute(
        Job(job_id="it-bundle", package_path=str(real_problem), submission_bundle_path=str(bundle))
    )
    _skip_without_sandbox(result, pytestconfig)

    assert result.status is JobStatus.SUCCEEDED, result.error
    assert result.report is not None
    assert result.report["slug"] == "sum-two"
    assert result.report["overall"] == "PASS"
    assert "submission.bundle.zip" in result.artifacts
    assert (attachments / "sum.hpp").is_file()
