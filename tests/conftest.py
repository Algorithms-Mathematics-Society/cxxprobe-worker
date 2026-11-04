from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from cxxprobe_worker.config import (
    JudgeConfig,
    MonitoringConfig,
    QueueConfig,
    StorageConfig,
    WorkerConfig,
    WorkspaceConfig,
)
from cxxprobe_worker.monitoring import build_logger
from cxxprobe_worker.queue import LocalJobQueue
from cxxprobe_worker.storage import FilesystemArtifactStorage
from cxxprobe_worker.workspace import WorkspaceManager

# The engine release the fleet runs. The deploy script is the one place it is
# pinned, so the tests read it from there rather than keeping a second copy
# that can drift.
_PIN_SOURCE = Path(__file__).parent.parent / "deploy" / "spot-user-data.sh"
_PIN = re.compile(r'CXXPROBE_VERSION="\$\{CXXPROBE_VERSION:-([^}"]+)\}"')


def _pinned_engine() -> str:
    match = _PIN.search(_PIN_SOURCE.read_text(encoding="utf-8"))
    return match.group(1) if match else "unknown"


def _engine_under_test() -> str:
    binary = os.environ.get("CXXPROBE_BINARY") or shutil.which("cxxprobe")
    if not binary:
        return "none (set CXXPROBE_BINARY; real-engine tests will skip)"
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{binary} (cannot run --version: {exc})"
    return f"{binary} {(out.stdout or out.stderr).strip() or '(no version output)'}"


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Name the engine at the end of every run, CI included.

    The report shape is cxxprobe's, and it changes between releases. When one
    of those changes broke an assertion here it surfaced as a single puzzling
    failure, with nothing in the output saying which engine the expectation
    had been written against. A summary section rather than a report header,
    because ``-q`` (set in ``addopts``) suppresses headers.
    """
    terminalreporter.section("cxxprobe engine")
    terminalreporter.write_line(
        f"pinned:     {_pinned_engine()} ({_PIN_SOURCE.relative_to(_PIN_SOURCE.parents[1])})"
    )
    terminalreporter.write_line(f"under test: {_engine_under_test()}")


@pytest.fixture
def logger():
    """A logger that writes nowhere, so tests stay quiet."""
    return build_logger(name="test", level="debug", log_format="text", stream=io.StringIO())


@pytest.fixture
def storage(tmp_path: Path) -> FilesystemArtifactStorage:
    return FilesystemArtifactStorage(tmp_path / "artifacts")


@pytest.fixture
def workspaces(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(tmp_path / "workspaces")


@pytest.fixture
def job_queue(tmp_path: Path) -> LocalJobQueue:
    return LocalJobQueue(tmp_path / "queue", visibility_timeout_seconds=60.0)


@pytest.fixture
def config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        environment="test",
        worker_id="worker-under-test",
        concurrency=1,
        judge=JudgeConfig(binary="cxxprobe", timeout_seconds=5.0),
        workspace=WorkspaceConfig(root=tmp_path / "workspaces"),
        storage=StorageConfig(root=tmp_path / "artifacts"),
        queue=QueueConfig(root=tmp_path / "queue", poll_interval_seconds=0.01),
        monitoring=MonitoringConfig(log_level="debug", log_format="text"),
    )


@pytest.fixture
def fake_cxxprobe(tmp_path: Path):
    """Builds a stand-in `cxxprobe` script with scriptable behaviour.

    The real binary needs a working sandbox (cgroups + user namespaces),
    which CI does not have — so the executor's contract is tested against a
    script that reproduces cxxprobe's documented exit-code/report behaviour
    exactly. What's under test here is the worker's interpretation of that
    contract, not cxxprobe itself.
    """

    def _build(*, exit_code: int, report: str | None, stderr: str = "", sleep: float = 0.0) -> Path:
        script = tmp_path / "fake-cxxprobe"
        write_report = ""
        if report is not None:
            # `cxxprobe judge` writes its report to the path after --output.
            write_report = (
                'out=""\n'
                'prev=""\n'
                'for arg in "$@"; do\n'
                '  if [ "$prev" = "--output" ]; then out="$arg"; fi\n'
                '  prev="$arg"\n'
                "done\n"
                'if [ -n "$out" ]; then cat > "$out" <<\'REPORT_EOF\'\n'
                f"{report}\n"
                "REPORT_EOF\n"
                "fi\n"
            )
        script.write_text(
            "#!/bin/sh\n"
            f"{'sleep ' + str(sleep) if sleep else ''}\n"
            f"{write_report}"
            f"{'printf %s ' + chr(39) + stderr + chr(39) + ' >&2' if stderr else ''}\n"
            f"exit {exit_code}\n"
        )
        script.chmod(0o755)
        return script

    return _build
