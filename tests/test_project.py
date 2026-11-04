"""Project jobs: several part submissions, one `cxxprobe test project` run.

The real subcommand is cxxprobe#6, so these run against a stand-in that
records its argv and prints the report that issue describes. What is under
test is the worker's side: the command it builds and how it reads the answer.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from cxxprobe_worker.config import JudgeConfig
from cxxprobe_worker.executor import JobExecutor
from cxxprobe_worker.jobs import Job, JobStatus

LABELS = ["1", "2", "3", "4", "5"]


def _part(verdict: str = "AC") -> dict[str, Any]:
    return {
        "overall": "PASS" if verdict == "AC" else "FAIL",
        "compile": {"solution": {"ok": True, "exit_code": 0}},
        "tests": {"manual": {"status": "PASS", "cases": [{"label": "1", "verdict": verdict}]}},
    }


PASSING = {
    "project": "order-desk",
    "overall": "PASS",
    "parts": {label: _part() for label in LABELS},
    "integration": {"status": "PASS", "compile": {"ok": True, "exit_code": 0}, "cases": []},
    "substitutions": [],
}

LINK_FAILURE = {
    "project": "order-desk",
    "overall": "ERROR",
    "parts": {label: _part() for label in LABELS},
    "integration": {
        "status": "ERROR",
        "compile": {
            "ok": False,
            "exit_code": 1,
            "diagnostics": "undefined reference to `Book::reserve(int)'",
        },
    },
    "substitutions": [],
}


def fake_engine(tmp_path: Path, report: dict[str, Any], exit_code: int = 0) -> tuple[Path, Path]:
    """A stand-in cxxprobe that logs each invocation and answers `test project`."""
    calls = tmp_path / "calls.log"
    body = tmp_path / "report.json"
    body.write_text(json.dumps(report))
    script = tmp_path / "fake-cxxprobe"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'if [ "$1" = "package" ]; then exit 0; fi\n'
        f'cat "{body}"\n'
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return script, calls


def make_project(tmp_path: Path, labels: list[str] = LABELS) -> tuple[Path, dict[str, str]]:
    contest = tmp_path / "round-2"
    contest.mkdir()
    (contest / "contest.yaml").write_text("version: 1\nname: Round 2\n")
    sources = {}
    for label in labels:
        src = tmp_path / f"part-{label}.cpp"
        src.write_text(f"// part {label}\n")
        sources[label] = str(src)
    return contest, sources


def executor_for(binary: Path, workspaces, storage, logger) -> JobExecutor:
    return JobExecutor(JudgeConfig(binary=str(binary)), workspaces, storage, logger)


def project_job(package: Path, parts: dict[str, str], **extra) -> Job:
    return Job(
        job_id="p1",
        job_type="project",
        project_slug="order-desk",
        package_path=str(package),
        parts=parts,
        **extra,
    )


def test_five_parts_become_one_test_project_run(tmp_path, workspaces, storage, logger):
    contest, parts = make_project(tmp_path)
    binary, calls = fake_engine(tmp_path, PASSING)

    result = executor_for(binary, workspaces, storage, logger).execute(project_job(contest, parts))

    assert result.status is JobStatus.SUCCEEDED
    assert result.report is not None
    assert set(result.report["parts"]) == set(LABELS)
    assert result.report["integration"]["status"] == "PASS"

    (argv,) = calls.read_text().splitlines()
    assert argv.startswith("test project order-desk --dir ")
    assert argv.endswith(" --json")
    for label in LABELS:
        assert f"--submission {label}=" in argv
        assert f"/parts/{label}.cpp" in argv


def test_fallback_is_passed_through_and_covers_a_missing_part(
    tmp_path, workspaces, storage, logger
):
    contest, parts = make_project(tmp_path, labels=["1", "2"])
    binary, calls = fake_engine(tmp_path, PASSING)

    result = executor_for(binary, workspaces, storage, logger).execute(
        project_job(contest, parts, fallback={"3": "reference"})
    )

    assert result.status is JobStatus.SUCCEEDED
    assert "--fallback 3=reference" in calls.read_text()


def test_a_zip_pack_is_unpacked_and_its_declared_parts_enforced(
    tmp_path, workspaces, storage, logger
):
    _, parts = make_project(tmp_path)
    pack = tmp_path / "round-2.cxxpkg"
    with zipfile.ZipFile(pack, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {"projects": [{"slug": "order-desk", "parts": [{"label": x} for x in LABELS]}]}
            ),
        )
    binary, calls = fake_engine(tmp_path, PASSING)
    executor = executor_for(binary, workspaces, storage, logger)

    assert executor.execute(project_job(pack, parts)).status is JobStatus.SUCCEEDED
    unpack, run = calls.read_text().splitlines()
    assert unpack.startswith(f"package unpack {Path(workspaces.root)}")
    assert run.startswith("test project order-desk")

    del parts["4"]
    refused = executor.execute(project_job(pack, parts))
    assert refused.status is JobStatus.FAILED
    assert "part 4" in (refused.error or "")


def test_parts_that_fail_to_link_are_a_verdict_with_the_report_kept(
    tmp_path, workspaces, storage, logger
):
    contest, parts = make_project(tmp_path)
    binary, _ = fake_engine(tmp_path, LINK_FAILURE, exit_code=2)

    result = executor_for(binary, workspaces, storage, logger).execute(project_job(contest, parts))

    # Exit 2 would ordinarily be a machine fault and retried. Parts that
    # will not link fail the same way everywhere, so it is terminal.
    assert result.status is JobStatus.FAILED
    assert "failed to link" in (result.error or "")
    stored = Path(result.artifacts["report.json"]).read_text()
    assert "undefined reference" in stored


def test_a_problem_jobs_command_line_is_unchanged(tmp_path, workspaces, storage, logger):
    package = tmp_path / "problem"
    package.mkdir()
    submission = tmp_path / "sub.cpp"
    submission.write_text("int main(){}\n")
    binary, calls = fake_engine(tmp_path, {"overall": "PASS"})

    executor_for(binary, workspaces, storage, logger).execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )

    (argv,) = calls.read_text().splitlines()
    words = argv.split(" ")
    assert words[0] == "judge"
    assert words[1] == "--problem-dir"
    assert words[3] == "--submission"
    assert words[5] == "--output"
    assert len(words) == 7
