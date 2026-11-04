"""Project jobs: several part submissions, one `cxxprobe test project` run.

The reports these tests feed back are not hand-written. tests/fixtures/project
holds `cxxprobe test project --json` output captured from the real engine
(cxxprobe main 6b2c958, the merge of cxxprobe#23) on a three-part project,
with only scratch paths rewritten and long compiler output trimmed:

    passing             every part supplied and correct          exit 0
    wrong_part          part 2 supplied but wrong                exit 1
    reference_fallback  part 3 not supplied, --fallback reference exit 0
    part_compile_error  part 2 does not compile                  exit 2
    link_failure        part 2 compiles alone, defines nothing   exit 2
    empty_fallback      part 2 linked as empty                   exit 2

A stand-in binary prints one of those and exits with the engine's code, so
what is under test is the worker's side: the command it builds, how it reads
the answer, and what it does with it. Hand-written reports of the shape
cxxprobe#6 described are what let the first version pass its own tests while
misreading every real report.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from cxxprobe_worker.config import JudgeConfig
from cxxprobe_worker.control_plane import summarise
from cxxprobe_worker.executor import JobExecutor
from cxxprobe_worker.jobs import Job, JobStatus

LABELS = ["1", "2", "3"]
FIXTURES = Path(__file__).parent / "fixtures" / "project"
EXIT_CODES = {
    "passing": 0,
    "wrong_part": 1,
    "reference_fallback": 0,
    "part_compile_error": 2,
    "link_failure": 2,
    "empty_fallback": 2,
}


def real_report(name: str) -> dict[str, Any]:
    report: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return report


def fake_engine(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A stand-in cxxprobe that logs each invocation and answers `test project`
    with a real report and the exit code the engine gave it."""
    calls = tmp_path / "calls.log"
    script = tmp_path / "fake-cxxprobe"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'if [ "$1" = "package" ]; then exit 0; fi\n'
        f'cat "{FIXTURES / name}.json"\n'
        f"exit {EXIT_CODES[name]}\n"
    )
    script.chmod(0o755)
    return script, calls


PROJECT_YAML = (
    "version: 1\nname: The Order Desk\nparts:\n"
    '  - {dir: parts/1-ingest, label: "1"}\n'
    '  - {dir: parts/2-book, label: "2"}\n'
    '  - {dir: parts/3-ship, label: "3"}\n'
)


def make_project(tmp_path: Path, labels: list[str] = LABELS) -> tuple[Path, dict[str, str]]:
    """An authored contest directory holding the project, and part sources."""
    contest = tmp_path / "round-2"
    (contest / "order-desk").mkdir(parents=True)
    (contest / "contest.yaml").write_text("version: 1\nname: Round 2\n")
    (contest / "order-desk" / "project.yaml").write_text(PROJECT_YAML)
    sources = {}
    for label in labels:
        src = tmp_path / f"part-{label}.cpp"
        src.write_text(f"// part {label}\n")
        sources[label] = str(src)
    return contest, sources


def make_pack(tmp_path: Path, labels: list[str] = LABELS) -> Path:
    pack = tmp_path / "round-2.cxxpkg"
    with zipfile.ZipFile(pack, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {"projects": [{"slug": "order-desk", "parts": [{"label": x} for x in labels]}]}
            ),
        )
    return pack


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


# ── jobs and the command line (#7, #8) ─────────────────────────────────


def test_parts_become_one_test_project_run(tmp_path, workspaces, storage, logger):
    contest, parts = make_project(tmp_path)
    binary, calls = fake_engine(tmp_path, "passing")

    result = executor_for(binary, workspaces, storage, logger).execute(project_job(contest, parts))

    assert result.status is JobStatus.SUCCEEDED
    assert result.report is not None
    assert [p["label"] for p in result.report["parts"]] == LABELS

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
    binary, calls = fake_engine(tmp_path, "reference_fallback")

    result = executor_for(binary, workspaces, storage, logger).execute(
        project_job(contest, parts, fallback={"3": "reference"})
    )

    assert result.status is JobStatus.SUCCEEDED
    assert "--fallback 3=reference" in calls.read_text()


@pytest.mark.parametrize("form", ["directory", "zip"])
def test_a_missing_part_is_refused_by_label_for_both_package_forms(
    form, tmp_path, workspaces, storage, logger
):
    contest, parts = make_project(tmp_path)
    package = contest if form == "directory" else make_pack(tmp_path)
    binary, _calls = fake_engine(tmp_path, "passing")
    executor = executor_for(binary, workspaces, storage, logger)

    assert executor.execute(project_job(package, parts)).status is JobStatus.SUCCEEDED

    del parts["2"]
    refused = executor.execute(project_job(package, parts))
    assert refused.status is JobStatus.FAILED
    assert "has no submission for part 2" in (refused.error or "")

    covered = executor.execute(project_job(package, parts, fallback={"2": "reference"}))
    assert covered.status is JobStatus.SUCCEEDED, covered.error


@pytest.mark.parametrize("form", ["directory", "zip"])
def test_unknown_labels_are_refused_for_both_package_forms(
    form, tmp_path, workspaces, storage, logger
):
    contest, parts = make_project(tmp_path)
    package = contest if form == "directory" else make_pack(tmp_path)
    binary, calls = fake_engine(tmp_path, "passing")
    executor = executor_for(binary, workspaces, storage, logger)

    parts["9"] = parts["1"]
    refused = executor.execute(project_job(package, parts))
    assert refused.status is JobStatus.FAILED
    assert "no part labelled 9" in (refused.error or "")

    del parts["9"]
    refused = executor.execute(project_job(package, parts, fallback={"8": "reference"}))
    assert refused.status is JobStatus.FAILED
    assert "no part labelled 8" in (refused.error or "")
    assert not calls.exists() or "test project" not in calls.read_text()


def test_a_package_that_does_not_declare_the_project_is_refused(
    tmp_path, workspaces, storage, logger
):
    contest, parts = make_project(tmp_path)
    (contest / "order-desk" / "project.yaml").unlink()
    binary, calls = fake_engine(tmp_path, "passing")

    refused = executor_for(binary, workspaces, storage, logger).execute(project_job(contest, parts))

    assert refused.status is JobStatus.FAILED
    assert "cannot verify part coverage" in (refused.error or "")
    assert not calls.exists()


def test_a_zip_pack_is_unpacked_first(tmp_path, workspaces, storage, logger):
    _, parts = make_project(tmp_path)
    binary, calls = fake_engine(tmp_path, "passing")

    result = executor_for(binary, workspaces, storage, logger).execute(
        project_job(make_pack(tmp_path), parts)
    )

    assert result.status is JobStatus.SUCCEEDED
    unpack, run = calls.read_text().splitlines()
    assert unpack.startswith(f"package unpack {Path(workspaces.root)}")
    assert run.startswith("test project order-desk")


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("part_compile_error", "part 2 failed to compile"),
        ("link_failure", "part 2 failed to compile"),
        ("empty_fallback", "project parts failed to link"),
    ],
)
def test_candidate_build_failures_are_terminal_with_the_report_kept(
    name, message, tmp_path, workspaces, storage, logger
):
    contest, parts = make_project(tmp_path)
    extra: dict[str, Any] = {}
    if name == "empty_fallback":
        del parts["2"]
        extra["fallback"] = {"2": "empty"}
    binary, _ = fake_engine(tmp_path, name)

    result = executor_for(binary, workspaces, storage, logger).execute(
        project_job(contest, parts, **extra)
    )

    # Exit 2 would ordinarily be a machine fault and retried. A part that
    # will not build, or parts that will not link, fail the same way on
    # every worker, so it is terminal and the report is kept.
    assert result.status is JobStatus.FAILED, result.error
    assert message in (result.error or "")
    assert "report.json" in result.artifacts


def test_a_link_step_the_sandbox_never_launched_is_retried(tmp_path, workspaces, storage, logger):
    contest, parts = make_project(tmp_path)
    del parts["2"]
    report = real_report("empty_fallback")
    report["integration"]["link"] = {"ok": False, "exit_code": -1, "diagnostics": "clone failed"}
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps(report))
    script = tmp_path / "fake-cxxprobe"
    script.write_text(f'#!/bin/sh\ncat "{custom}"\nexit 2\n')
    script.chmod(0o755)

    result = executor_for(script, workspaces, storage, logger).execute(
        project_job(contest, parts, fallback={"2": "empty"})
    )

    assert result.status is JobStatus.RETRYABLE


def test_a_problem_jobs_command_line_is_unchanged(tmp_path, workspaces, storage, logger):
    package = tmp_path / "problem"
    package.mkdir()
    submission = tmp_path / "sub.cpp"
    submission.write_text("int main(){}\n")
    calls = tmp_path / "calls.log"
    script = tmp_path / "fake-cxxprobe"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexit 0\n')
    script.chmod(0o755)

    executor_for(script, workspaces, storage, logger).execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )

    (argv,) = calls.read_text().splitlines()
    words = argv.split(" ")
    assert words[0] == "judge"
    assert words[1] == "--problem-dir"
    assert words[3] == "--submission"
    assert words[5] == "--output"
    assert len(words) == 7


def test_a_label_with_a_trailing_newline_is_not_a_plain_name():
    with pytest.raises(ValueError, match="not a plain name"):
        Job(
            job_id="p1",
            job_type="project",
            project_slug="order-desk",
            package_path="/x",
            parts={"1\n": "/a.cpp"},
        )


# ── results: attribution to the part that caused a failure (#9) ──────────


def test_a_passing_project_keeps_every_count():
    out = summarise(real_report("passing"))

    assert out["verdict"] == "AC"
    assert set(out["parts"]) == set(LABELS)
    assert all(p["verdict"] == "AC" and p["behavior"] == "PASS" for p in out["parts"].values())
    assert all(p["total_count"] == 1 for p in out["parts"].values())
    assert out["total_count"] == 3
    assert {c["part"] for c in out["testcases"]} == set(LABELS)
    assert out["integration"]["verdict"] == "AC"
    assert out["integration"]["passed_count"] == out["integration"]["total_count"] == 3
    assert out["failing_parts"] == []
    assert out["substitutions"] == []
    assert out["project_failure"] is False


def test_a_wrong_part_is_named_and_the_others_keep_their_own_verdicts():
    out = summarise(real_report("wrong_part"))

    assert out["verdict"] == "WA"
    assert out["failing_parts"] == ["2"]
    assert out["parts"]["2"]["verdict"] == "WA"
    assert out["parts"]["2"]["behavior"] == "FAIL"
    assert out["parts"]["1"]["verdict"] == out["parts"]["3"]["verdict"] == "AC"
    assert out["integration"]["verdict"] == "WA"
    assert out["integration"]["failed_parts"] == {"2": ["Integration.Part2_Book"]}
    assert out["integration"]["unattributed"] == ["Integration.EndToEnd"]
    assert out["project_failure"] is False


def test_a_reference_stand_in_is_visible_in_the_result():
    out = summarise(real_report("reference_fallback"))

    assert out["verdict"] == "AC"
    assert out["substitutions"] == [{"label": "3", "origin": "reference", "reason": "not supplied"}]
    assert out["parts"]["3"]["substituted"] == "reference"
    assert out["parts"]["3"]["origin"] == "reference"
    assert out["parts"]["1"]["substituted"] is None


def test_a_part_that_does_not_compile_is_ce_on_that_part():
    out = summarise(real_report("part_compile_error"))

    assert out["verdict"] == "CE"
    assert out["parts"]["2"]["verdict"] == "CE"
    assert out["parts"]["2"]["compile_output"]
    assert out["parts"]["1"]["verdict"] == out["parts"]["3"]["verdict"] == "AC"
    assert out["integration"]["verdict"] == "CE"
    assert out["failing_parts"] == ["2"]
    assert out["project_failure"] is False


def test_a_part_that_defines_nothing_is_blamed_not_the_project():
    out = summarise(real_report("link_failure"))

    assert out["integration"]["verdict"] == "CE"
    assert "undefined reference" in out["integration"]["diagnostics"]
    assert out["failing_parts"] == ["2"]
    assert out["project_failure"] is False


def test_an_empty_part_has_no_verdict_and_the_link_failure_is_the_projects():
    out = summarise(real_report("empty_fallback"))

    assert out["parts"]["2"]["verdict"] is None
    assert out["parts"]["2"]["substituted"] == "empty"
    assert out["integration"]["verdict"] == "CE"
    assert out["failing_parts"] == []
    assert out["project_failure"] is True


def test_part_artifacts_are_keyed_by_label(tmp_path, workspaces, storage, logger):
    contest, parts = make_project(tmp_path)
    binary, _ = fake_engine(tmp_path, "part_compile_error")

    result = executor_for(binary, workspaces, storage, logger).execute(project_job(contest, parts))

    for label in LABELS:
        assert f"parts/{label}/submission.cpp" in result.artifacts
    log = Path(result.artifacts["parts/2/compile.log"]).read_text()
    assert "[behavior_binary]" in log
    assert "[integration]" in log
    assert "parts/1/compile.log" not in result.artifacts
