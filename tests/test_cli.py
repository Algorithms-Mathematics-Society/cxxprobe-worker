from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cxxprobe_worker.cli import app

runner = CliRunner()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "test.yaml").write_text(
        "worker_id: cli-test\n"
        "concurrency: 1\n"
        f"workspace:\n  root: {tmp_path / 'ws'}\n"
        f"storage:\n  root: {tmp_path / 'art'}\n"
        f"queue:\n  root: {tmp_path / 'q'}\n  poll_interval_seconds: 0.01\n"
        "monitoring:\n  log_level: error\n  log_format: text\n"
    )
    return d


def test_missing_config_exits_two(tmp_path: Path):
    result = runner.invoke(app, ["status", "--config-dir", str(tmp_path), "--env", "nope"])
    assert result.exit_code == 2


def test_status_reports_queue_depth(config_dir: Path):
    result = runner.invoke(app, ["status", "--config-dir", str(config_dir)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["worker_id"] == "cli-test"
    assert payload["queue_depth"] == 0


def test_submit_then_status_shows_the_queued_job(config_dir: Path, tmp_path: Path):
    package = tmp_path / "pkg"
    package.mkdir()
    submission = tmp_path / "s.cpp"
    submission.write_text("int main(){}\n")

    submitted = runner.invoke(
        app,
        [
            "submit",
            "--config-dir",
            str(config_dir),
            "--package",
            str(package),
            "--submission",
            str(submission),
            "--job-id",
            "job-xyz",
        ],
    )
    assert submitted.exit_code == 0, submitted.stdout
    assert "job-xyz" in submitted.stdout

    status = runner.invoke(app, ["status", "--config-dir", str(config_dir)])
    assert json.loads(status.stdout)["queue_depth"] == 1


def test_once_on_empty_queue_exits_three(config_dir: Path):
    # Distinguishes "nothing to do" from "something went wrong" for a
    # cron-style caller.
    result = runner.invoke(app, ["once", "--config-dir", str(config_dir)])
    assert result.exit_code == 3


def test_once_runs_a_queued_job(config_dir: Path, tmp_path: Path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "problem.yaml").write_text("version: 2\nname: A\n")
    submission = tmp_path / "s.cpp"
    submission.write_text("int main(){}\n")

    fake = tmp_path / "fake-cxxprobe"
    fake.write_text(
        "#!/bin/sh\n"
        'out=""; prev=""\n'
        'for a in "$@"; do if [ "$prev" = "--output" ]; then out="$a"; fi; prev="$a"; done\n'
        'printf \'{"overall": "PASS"}\' > "$out"\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    (config_dir / "test.yaml").write_text(
        (config_dir / "test.yaml").read_text() + f"judge:\n  binary: {fake}\n"
    )

    runner.invoke(
        app,
        [
            "submit",
            "--config-dir",
            str(config_dir),
            "--package",
            str(package),
            "--submission",
            str(submission),
            "--job-id",
            "job-1",
        ],
    )
    result = runner.invoke(app, ["once", "--config-dir", str(config_dir)])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["status"] == "succeeded"


def test_doctor_reports_a_missing_binary(config_dir: Path, tmp_path: Path):
    (config_dir / "test.yaml").write_text(
        (config_dir / "test.yaml").read_text()
        + f"judge:\n  binary: {tmp_path / 'definitely-not-here'}\n"
    )
    result = runner.invoke(app, ["doctor", "--config-dir", str(config_dir)])
    assert result.exit_code == 1


def test_doctor_passes_with_a_present_binary(config_dir: Path):
    (config_dir / "test.yaml").write_text(
        (config_dir / "test.yaml").read_text() + "judge:\n  binary: /bin/echo\n"
    )
    result = runner.invoke(app, ["doctor", "--config-dir", str(config_dir)])
    assert result.exit_code == 0, result.stdout
    assert "OK" in result.stdout
