"""Exact engine identities, never labels or first-part fallback."""

from pathlib import Path

import pytest

from cxxprobe_worker.config import JudgeConfig
from cxxprobe_worker.executor import JobExecutor
from cxxprobe_worker.jobs import Job, JobStatus


def project_tree(tmp_path: Path) -> Path:
    root = tmp_path / "contest"
    project = root / "desk"
    project.mkdir(parents=True)
    (root / "contest.yaml").write_text("version: 1\nname: Contest\n")
    (project / "project.yaml").write_text(
        "version: 1\nname: Desk\nparts:\n"
        "  - {dir: parts/alpha, label: A}\n"
        "  - {dir: nested/beta, label: B}\n"
    )
    for part in ("parts/alpha", "nested/beta"):
        path = project / part
        path.mkdir(parents=True)
        (path / "problem.yaml").write_text("version: 2\nname: Part\n")
    return root


@pytest.mark.parametrize("selector", ["desk/beta", "desk/alpha"])
@pytest.mark.parametrize("custom", [False, True])
def test_exact_part_directory_is_sent_to_judge(
    tmp_path, workspaces, storage, logger, fake_cxxprobe, monkeypatch, selector, custom
):
    root = project_tree(tmp_path)
    submission = tmp_path / "source.cpp"
    submission.write_text("int main(){}")
    binary = fake_cxxprobe(exit_code=0, report='{"overall":"PASS"}')
    executor = JobExecutor(JudgeConfig(binary=str(binary)), workspaces, storage, logger)
    original = executor._invoke
    calls = []

    def invoke(job, started, argv):
        calls.append(argv)
        selected = Path(argv[argv.index("--problem-dir") + 1])
        expected = "nested/beta" if selector == "desk/beta" else "parts/alpha"
        assert selected.as_posix().endswith("desk/" + expected)
        assert (selected / "problem.yaml").is_file()
        assert (selected.parents[1] / "project.yaml").is_file()
        return original(job, started, argv)

    monkeypatch.setattr(executor, "_invoke", invoke)
    result = executor.execute(
        Job(
            job_id="selection",
            package_path=str(root),
            submission_path=str(submission),
            problem_slug=selector,
            custom_cases=[{"input": ""}] if custom else None,
        )
    )
    assert result.status is JobStatus.SUCCEEDED
    assert len(calls) == 1
    assert ("--custom-cases" in calls[0]) is custom


@pytest.mark.parametrize(
    "selector", ["desk/missing", "B", "beta", "../beta", "", "desk/parts/alpha"]
)
def test_unknown_selector_is_permanent_without_invocation(
    tmp_path, workspaces, storage, logger, monkeypatch, selector
):
    root = project_tree(tmp_path)
    submission = tmp_path / "source.cpp"
    submission.write_text("int main(){}")
    executor = JobExecutor(JudgeConfig(), workspaces, storage, logger)

    def forbidden(*args):
        pytest.fail("unknown selector invoked the judge")

    monkeypatch.setattr(executor, "_invoke", forbidden)
    result = executor.execute(
        Job(
            job_id="unknown",
            package_path=str(root),
            submission_path=str(submission),
            problem_slug=selector,
        )
    )
    assert result.status is JobStatus.FAILED
    assert not result.should_retry
    assert "selector" in (result.error or "")


@pytest.mark.parametrize("fault", ["duplicate", "traversal", "symlink"])
def test_invalid_project_resolution_is_rejected(
    tmp_path, fault, workspaces, storage, logger, monkeypatch
):
    from cxxprobe_worker.executor import PreparationError, _select_problem

    root = project_tree(tmp_path)
    project = root / "desk"
    if fault == "duplicate":
        extra = project / "another/beta"
        extra.mkdir(parents=True)
        (extra / "problem.yaml").write_text("version: 2\nname: Duplicate\n")
        with (project / "project.yaml").open("a") as stream:
            stream.write("  - {dir: another/beta, label: C}\n")
    else:
        outside = root / "outside"
        outside.mkdir()
        (outside / "problem.yaml").write_text("version: 2\nname: Outside\n")
        if fault == "symlink":
            (project / "escape").symlink_to(outside, target_is_directory=True)
        directory = "escape" if fault == "symlink" else "../outside"
        with (project / "project.yaml").open("a") as stream:
            stream.write(f"  - {{dir: {directory}, label: C}}\n")
    with pytest.raises(PreparationError, match="selector"):
        _select_problem(root, "desk/beta")
    source = tmp_path / "source.cpp"
    source.write_text("int main(){}")
    executor = JobExecutor(JudgeConfig(), workspaces, storage, logger)

    def forbidden(*args):
        pytest.fail("invalid project reached engine invocation")

    monkeypatch.setattr(executor, "_invoke", forbidden)
    result = executor.execute(
        Job(
            job_id="invalid",
            package_path=str(root),
            submission_path=str(source),
            problem_slug="desk/beta",
        )
    )
    assert result.status is JobStatus.FAILED
    assert not result.should_retry


@pytest.mark.parametrize("selector", [None, "plain", "other"])
def test_plain_problem_compatibility(
    tmp_path, workspaces, storage, logger, fake_cxxprobe, selector
):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "problem.yaml").write_text("version: 2\nname: Plain\n")
    source = tmp_path / "source.cpp"
    source.write_text("int main(){}")
    binary = fake_cxxprobe(exit_code=0, report='{"overall":"PASS"}')
    result = JobExecutor(JudgeConfig(binary=str(binary)), workspaces, storage, logger).execute(
        Job(
            job_id="plain",
            package_path=str(root),
            submission_path=str(source),
            problem_slug=selector,
        )
    )
    assert result.status is (JobStatus.FAILED if selector == "other" else JobStatus.SUCCEEDED)
