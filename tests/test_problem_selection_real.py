"""Real multipart package selection through unpack, shared compilation and judge."""

import shutil
import subprocess

import pytest
import yaml

from cxxprobe_worker.jobs import Job, JobStatus
from tests.test_integration import build_executor
from tests.test_integration import real_problem as real_problem


@pytest.fixture
def multipart(real_problem, real_engine_binary):
    contest = real_problem.parent
    project = contest / "desk"
    for part in ("parts/alpha", "nested/beta"):
        shutil.copytree(real_problem, project / part)
    (project / "project.yaml").write_text(
        "version: 1\nname: Desk\nshared:\n  sources: [offset.cpp]\nparts:\n"
        "  - {dir: parts/alpha, label: A}\n"
        "  - {dir: nested/beta, label: B}\n"
    )
    shared = project / "shared"
    shared.mkdir()
    (shared / "offset.hpp").write_text("int offset(); int part_value();\n")
    (shared / "offset.cpp").write_text("int offset(){return 1;}\n")
    source = (
        '#include "offset.hpp"\n#include <iostream>\n'
        'int main(){std::cout<<6+offset()+part_value()<<"\\n";}\n'
    )
    for part, value in (("parts/alpha", 1), ("nested/beta", 0)):
        path = project / part
        (path / "part.cpp").write_text(f"int part_value(){{return {value};}}\n")
        config = yaml.safe_load((path / "problem.yaml").read_text())
        config["compiler"]["extra_sources"] = ["part.cpp"]
        (path / "problem.yaml").write_text(yaml.safe_dump(config))
        (path / "solutions/main.cpp").write_text(source)
    # The same submission prints 8 for alpha and 7 for beta, through each
    # selected part's extra source. Alpha's hidden answer deliberately differs.
    (project / "parts/alpha/tests/1.ans").write_text("9\n")
    submission = contest / "candidate.cpp"
    submission.write_text(source)
    archive = contest / "project.zip"
    packed = subprocess.run(
        [real_engine_binary, "package", "pack", "--problems", "desk", "-o", str(archive)],
        cwd=contest,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert packed.returncode == 0, packed.stderr
    return contest, archive, submission


@pytest.mark.parametrize("kind", ["judge", "run", "rejudge"])
@pytest.mark.parametrize("packaged", [False, True])
def test_real_selected_part(multipart, real_engine_binary, tmp_path, logger, kind, packaged):
    contest, archive, submission = multipart
    executor = build_executor(tmp_path, logger, real_engine_binary)
    for selector, verdict in (("desk/beta", "PASS"), ("desk/alpha", "FAIL")):
        result = executor.execute(
            Job(
                job_id=f"{kind}-{selector.split('/')[-1]}",
                package_path=str(archive if packaged else contest),
                submission_path=str(submission),
                problem_slug=selector,
                custom_cases=[
                    {"input": "", "expected": "8\n" if selector == "desk/alpha" else "7\n"}
                ]
                if kind == "run"
                else None,
                metadata={"rejudge": True} if kind == "rejudge" else {},
            )
        )
        assert result.status is JobStatus.SUCCEEDED, result.error
        assert result.report is not None
        if kind != "run":
            assert result.report["slug"] == selector
            assert result.report["overall"] == verdict
        else:
            assert result.report["status"] == "PASS"
            assert result.report["cases"][0]["stdout"] == (
                "8\n" if selector == "desk/alpha" else "7\n"
            )
        assert "report.json" in result.artifacts


def test_real_unknown_pack_selector_never_judges(multipart, real_engine_binary, tmp_path, logger):
    _, archive, submission = multipart
    result = build_executor(tmp_path, logger, real_engine_binary).execute(
        Job(
            job_id="unknown",
            package_path=str(archive),
            submission_path=str(submission),
            problem_slug="desk/not-declared",
        )
    )
    assert result.status is JobStatus.FAILED
    assert result.report is None
    assert not result.should_retry
    assert "selector" in (result.error or "")
