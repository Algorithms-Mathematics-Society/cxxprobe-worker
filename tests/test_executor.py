from __future__ import annotations

import json
from pathlib import Path

from cxxprobe_worker.config import JudgeConfig
from cxxprobe_worker.executor import JobExecutor
from cxxprobe_worker.jobs import Job, JobStatus
from cxxprobe_worker.storage import FilesystemArtifactStorage
from cxxprobe_worker.workspace import WorkspaceManager

SAMPLE_REPORT = json.dumps({"slug": "a-warmup", "overall": "PASS"})
FAILING_REPORT = json.dumps({"slug": "a-warmup", "overall": "FAIL"})


def make_inputs(tmp_path: Path) -> tuple[Path, Path]:
    """A problem directory and a submission, both minimally plausible."""
    package = tmp_path / "problem"
    package.mkdir()
    (package / "problem.yaml").write_text("version: 2\nname: A\n")
    submission = tmp_path / "sub.cpp"
    submission.write_text("int main(){}\n")
    return package, submission


def build_executor(
    binary: Path | str,
    workspaces: WorkspaceManager,
    storage: FilesystemArtifactStorage,
    logger,
    timeout: float = 10.0,
) -> JobExecutor:
    return JobExecutor(
        JudgeConfig(binary=str(binary), timeout_seconds=timeout), workspaces, storage, logger
    )


def test_exit_zero_with_report_is_succeeded(tmp_path, fake_cxxprobe, workspaces, storage, logger):
    package, submission = make_inputs(tmp_path)
    binary = fake_cxxprobe(exit_code=0, report=SAMPLE_REPORT)
    executor = build_executor(binary, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    assert result.status is JobStatus.SUCCEEDED
    assert result.report == {"slug": "a-warmup", "overall": "PASS"}


def test_exit_one_with_report_is_still_succeeded(
    tmp_path, fake_cxxprobe, workspaces, storage, logger
):
    # A WA submission is a *successful* job. Retrying it would be pointless
    # and would re-bill the same work forever.
    package, submission = make_inputs(tmp_path)
    binary = fake_cxxprobe(exit_code=1, report=FAILING_REPORT)
    executor = build_executor(binary, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    assert result.status is JobStatus.SUCCEEDED
    assert result.should_retry is False
    assert result.report is not None
    assert result.report["overall"] == "FAIL"


def test_exit_two_without_report_is_retryable(tmp_path, fake_cxxprobe, workspaces, storage, logger):
    package, submission = make_inputs(tmp_path)
    binary = fake_cxxprobe(exit_code=2, report=None, stderr="cxxprobe: bad problem dir")
    executor = build_executor(binary, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    assert result.status is JobStatus.RETRYABLE
    assert result.error is not None
    assert "bad problem dir" in result.error


def test_judged_exit_code_without_report_is_a_permanent_failure(
    tmp_path, fake_cxxprobe, workspaces, storage, logger
):
    # Exit 0/1 means judging happened, so a missing report is cxxprobe
    # contradicting its own contract — retrying can't fix that.
    package, submission = make_inputs(tmp_path)
    binary = fake_cxxprobe(exit_code=1, report=None)
    executor = build_executor(binary, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    assert result.status is JobStatus.FAILED


def test_exit_two_with_a_report_is_retryable_not_a_verdict(
    tmp_path, fake_cxxprobe, workspaces, storage, logger
):
    # cxxprobe writes a report on exit 2 too — on a host with no usable
    # sandbox it emits overall=ERROR. Recording that as a verdict would lose
    # a submission that was never actually run.
    package, submission = make_inputs(tmp_path)
    unjudgeable = json.dumps(
        {
            "slug": "a-warmup",
            "overall": "ERROR",
            "compile": {
                "solution": {
                    "ok": False,
                    "diagnostics": "create cgroup root /sys/fs/cgroup/cxxprobe: Permission denied",
                }
            },
        }
    )
    binary = fake_cxxprobe(exit_code=2, report=unjudgeable)
    executor = build_executor(binary, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    assert result.status is JobStatus.RETRYABLE
    assert result.should_retry is True
    assert result.error is not None
    # The specific diagnostic is surfaced, not just a bare exit code.
    assert "Permission denied" in result.error
    # The report is still kept, because it's what explains the failure.
    assert result.report is not None
    assert "report.json" in result.artifacts


def test_missing_binary_is_retryable(tmp_path, workspaces, storage, logger):
    package, submission = make_inputs(tmp_path)
    executor = build_executor(tmp_path / "does-not-exist", workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    assert result.status is JobStatus.RETRYABLE
    assert result.error is not None
    assert "not found" in result.error


def test_timeout_is_retryable(tmp_path, fake_cxxprobe, workspaces, storage, logger):
    package, submission = make_inputs(tmp_path)
    binary = fake_cxxprobe(exit_code=0, report=SAMPLE_REPORT, sleep=5)
    executor = build_executor(binary, workspaces, storage, logger, timeout=0.2)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    assert result.status is JobStatus.RETRYABLE
    assert result.error is not None
    assert "exceeded" in result.error


def test_missing_submission_is_a_permanent_failure(
    tmp_path, fake_cxxprobe, workspaces, storage, logger
):
    package, _ = make_inputs(tmp_path)
    binary = fake_cxxprobe(exit_code=0, report=SAMPLE_REPORT)
    executor = build_executor(binary, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(tmp_path / "absent.cpp"))
    )
    assert result.status is JobStatus.FAILED
    assert result.error is not None
    assert "submission not found" in result.error


def test_package_that_is_neither_zip_nor_directory_fails_permanently(
    tmp_path, fake_cxxprobe, workspaces, storage, logger
):
    _, submission = make_inputs(tmp_path)
    bogus = tmp_path / "not-a-package.txt"
    bogus.write_text("hello")
    binary = fake_cxxprobe(exit_code=0, report=SAMPLE_REPORT)
    executor = build_executor(binary, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(bogus), submission_path=str(submission))
    )
    assert result.status is JobStatus.FAILED


def test_zip_package_is_passed_with_the_package_flag(tmp_path, workspaces, storage, logger):
    import zipfile

    _, submission = make_inputs(tmp_path)
    zip_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("manifest.json", "{}")

    # Echo the argv into the report so the test can assert on the flag used.
    recorder = tmp_path / "recorder"
    recorder.write_text(
        "#!/bin/sh\n"
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "--output" ]; then out="$a"; fi; prev="$a"; done\n'
        'printf \'{"argv": "%s"}\' "$*" > "$out"\n'
        "exit 0\n"
    )
    recorder.chmod(0o755)
    executor = build_executor(recorder, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(zip_path), submission_path=str(submission))
    )
    assert result.status is JobStatus.SUCCEEDED
    assert result.report is not None
    assert "--package" in result.report["argv"]


def test_directory_package_is_passed_with_the_problem_dir_flag(
    tmp_path, workspaces, storage, logger
):
    package, submission = make_inputs(tmp_path)
    recorder = tmp_path / "recorder"
    recorder.write_text(
        "#!/bin/sh\n"
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "--output" ]; then out="$a"; fi; prev="$a"; done\n'
        'printf \'{"argv": "%s"}\' "$*" > "$out"\n'
        "exit 0\n"
    )
    recorder.chmod(0o755)
    executor = build_executor(recorder, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    assert result.report is not None
    assert "--problem-dir" in result.report["argv"]


def test_artifacts_are_persisted_for_a_successful_job(
    tmp_path, fake_cxxprobe, workspaces, storage, logger
):
    package, submission = make_inputs(tmp_path)
    binary = fake_cxxprobe(exit_code=0, report=SAMPLE_REPORT, stderr="some warning")
    executor = build_executor(binary, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    assert "report.json" in result.artifacts
    assert "submission.cpp" in result.artifacts
    assert "stderr.log" in result.artifacts
    assert storage.get_bytes("j1", "submission.cpp") == b"int main(){}\n"


def test_artifacts_outlive_the_workspace(tmp_path, fake_cxxprobe, workspaces, storage, logger):
    package, submission = make_inputs(tmp_path)
    binary = fake_cxxprobe(exit_code=0, report=SAMPLE_REPORT)
    executor = build_executor(binary, workspaces, storage, logger)

    executor.execute(Job(job_id="j1", package_path=str(package), submission_path=str(submission)))
    # The workspace is gone, but the report is still readable — that's the
    # entire point of the split between the two.
    assert list(workspaces.root.iterdir()) == []
    assert storage.exists("j1", "report.json")


def test_unparseable_report_is_treated_as_no_report(tmp_path, workspaces, storage, logger):
    _, submission = make_inputs(tmp_path)
    package = tmp_path / "problem"
    garbage = tmp_path / "garbage-report"
    garbage.write_text(
        "#!/bin/sh\n"
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "--output" ]; then out="$a"; fi; prev="$a"; done\n'
        'printf "not json at all" > "$out"\n'
        "exit 0\n"
    )
    garbage.chmod(0o755)
    executor = build_executor(garbage, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    assert result.status is JobStatus.FAILED


def test_a_submission_that_will_not_compile_is_permanent_not_retryable(
    tmp_path, fake_cxxprobe, workspaces, storage, logger
):
    """A syntax error fails identically on every worker.

    cxxprobe exits 2 both for a broken machine and for a broken submission.
    Treating the second as retryable means a contest's compile errors — a
    large share of all submissions — recirculate until they dead-letter,
    starving real work.
    """
    package, submission = make_inputs(tmp_path)
    ce_report = json.dumps(
        {
            "slug": "a-warmup",
            "overall": "ERROR",
            "compile": {"solution": {"ok": False, "exit_code": 1, "diagnostics": "expected ';'"}},
        }
    )
    binary = fake_cxxprobe(exit_code=2, report=ce_report)
    executor = build_executor(binary, workspaces, storage, logger)

    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    assert result.status is JobStatus.FAILED
    assert result.should_retry is False
    assert result.error is not None
    assert "failed to compile" in result.error


def test_a_broken_sandbox_is_still_retryable(tmp_path, fake_cxxprobe, workspaces, storage, logger):
    """Same exit code and report shape as a CE — but exit_code -1 gives it away."""
    package, submission = make_inputs(tmp_path)
    sandbox_report = json.dumps(
        {
            "slug": "a-warmup",
            "overall": "ERROR",
            "compile": {
                "solution": {
                    "ok": False,
                    "exit_code": -1,
                    "diagnostics": "create cgroup root /sys/fs/cgroup/cxxprobe: Permission denied",
                }
            },
        }
    )
    binary = fake_cxxprobe(exit_code=2, report=sandbox_report)
    executor = build_executor(binary, workspaces, storage, logger)
    result = executor.execute(
        Job(job_id="j1", package_path=str(package), submission_path=str(submission))
    )
    # Same shape as a compile error, but exit_code -1 means the compiler
    # never ran — that is the machine's fault, so it must be retried.
    assert result.status is JobStatus.RETRYABLE
    assert result.should_retry is True


def test_a_submission_that_crashes_a_test_run_is_not_retried():
    """The same class of bug as retrying compile errors, and it bites harder.

    A use-after-free makes the GTest binary die: `behavior: ERROR`, zero
    cases, cxxprobe exits 2 — the same exit code as a host with no usable
    sandbox. Retrying it re-runs an identical failure until the job
    dead-letters, leaving the submission stuck at "queued" for ever.

    In a contest about RAII and manual memory this is among the most likely
    things a contestant writes.
    """
    from cxxprobe_worker.executor import _submission_crashed_at_runtime

    crashed = {
        "compile": {"solution": {"ok": True}, "behavior_binary": {"ok": True}},
        "tests": {"manual": {"status": "PASS"}, "behavior": {"status": "ERROR"}},
    }
    assert _submission_crashed_at_runtime(crashed) is True


def test_a_broken_sandbox_is_still_retried():
    """The distinction that makes the above safe: if a compile step never
    launched, the machine is at fault and another worker may well succeed."""
    from cxxprobe_worker.executor import _submission_crashed_at_runtime

    broken_host = {
        "compile": {"behavior_binary": {"ok": False, "exit_code": -1}},
        "tests": {"behavior": {"status": "ERROR"}},
    }
    assert _submission_crashed_at_runtime(broken_host) is False


def test_a_clean_run_is_not_mistaken_for_a_crash():
    from cxxprobe_worker.executor import _submission_crashed_at_runtime

    fine = {
        "compile": {"solution": {"ok": True}},
        "tests": {"manual": {"status": "PASS"}, "behavior": {"status": "FAIL"}},
    }
    assert _submission_crashed_at_runtime(fine) is False, "FAIL is a verdict, not a crash"
