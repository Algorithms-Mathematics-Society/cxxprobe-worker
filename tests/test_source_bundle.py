"""Bundle staging preserves every candidate unit and rejects archive aliases."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from cxxprobe_worker.config import JudgeConfig
from cxxprobe_worker.executor import JobExecutor, _stage_parts
from cxxprobe_worker.jobs import Job
from cxxprobe_worker.source_bundle import BundleError, unpack
from cxxprobe_worker.storage import FilesystemArtifactStorage
from cxxprobe_worker.workspace import WorkspaceManager


def archive(files, *, entry="main.cpp", units=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": "ams-source-bundle-v1",
                    "entrypoint": entry,
                    "compilation_units": units if units is not None else [entry],
                }
            ),
        )
        for path, content in files.items():
            zf.writestr("files/" + path, content)
    return out.getvalue()


def test_stages_units_headers_and_explicit_manifest(tmp_path):
    src = tmp_path / "input.zip"
    src.write_bytes(
        archive(
            {
                "main.cpp": '#include "lib.hpp"\nint main(){return answer();}',
                "lib.cpp": '#include "lib.hpp"\nint answer(){return 42;}',
                "lib.hpp": "int answer();",
            },
            units=["main.cpp", "lib.cpp"],
        )
    )
    entry = unpack(src, tmp_path / "staged")
    assert entry.read_text().startswith('#include "lib.hpp"')
    assert (tmp_path / "staged/lib.cpp").is_file()
    manifest = json.loads((tmp_path / "staged/main.cpp.cxxprobe.json").read_text())
    assert manifest["units"] == ["lib.cpp", "main.cpp"]
    assert manifest["files"] == ["lib.cpp", "lib.hpp", "main.cpp"]


@pytest.mark.parametrize(
    "files,units",
    [
        ({"../escape.cpp": ""}, ["../escape.cpp"]),
        ({"main.cpp": "", "MAIN.cpp": ""}, ["main.cpp"]),
        ({"main.cpp": ""}, [["main.cpp"]]),
        ({"main.cpp": "", "ref.hpp": ""}, ["ref.hpp"]),
    ],
)
def test_rejects_bad_bundle(tmp_path, files, units):
    src = tmp_path / "input.zip"
    src.write_bytes(archive(files, units=units))
    with pytest.raises(BundleError):
        unpack(src, tmp_path / "staged")


def test_project_part_bundle_stages_complete_file_set(tmp_path):
    src = tmp_path / "source.bundle.zip"
    src.write_bytes(archive({"main.cpp": '#include "lib.hpp"', "lib.hpp": "int answer();"}))
    job = Job(
        job_id="job",
        job_type="project",
        package_path=str(tmp_path / "pack"),
        project_slug="project",
        parts={"A": str(src)},
    )
    (tmp_path / "work").mkdir()
    with WorkspaceManager(tmp_path / "work").session("job") as workspace:
        staged = _stage_parts(job, workspace)
        assert staged["A"].read_text() == '#include "lib.hpp"'
        assert (workspace.parts_dir / "A/lib.hpp").is_file()
        assert (workspace.parts_dir / "A/main.cpp.cxxprobe.json").is_file()


def test_problem_bundle_reaches_judge_with_capability_flag(tmp_path, logger):
    package = tmp_path / "problem"
    package.mkdir()
    (package / "problem.yaml").write_text("version: 2\nname: Example\n")
    src = tmp_path / "source.bundle.zip"
    src.write_bytes(
        archive(
            {
                "main.cpp": "int main(){return answer();}",
                "helper.cpp": "int answer(){return 42;}",
                "helper.hpp": "int answer();",
            },
            units=["main.cpp", "helper.cpp"],
        )
    )
    calls = tmp_path / "calls.log"
    engine = tmp_path / "engine"
    engine.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" > "{calls}"\n'
        'prev=""; for arg in "$@"; do\n'
        '  if [ "$prev" = "--output" ]; then\n'
        '    printf \'{"overall":"PASS"}\' > "$arg"\n'
        '  fi\n'
        '  prev="$arg"\n'
        'done\n'
        "exit 0\n"
    )
    engine.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    executor = JobExecutor(
        JudgeConfig(binary=str(engine)),
        WorkspaceManager(work),
        FilesystemArtifactStorage(tmp_path / "artifacts"),
        logger,
    )
    result = executor.execute(
        Job(job_id="bundle", package_path=str(package), submission_bundle_path=str(src))
    )
    assert result.status.value == "succeeded"
    invocation = calls.read_text()
    assert "--candidate-bundle" in invocation
    assert "/submission-files/main.cpp" in invocation
    assert "submission.bundle.zip" in result.artifacts
