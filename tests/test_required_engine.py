"""Subprocess checks of the pytest gate; stand-ins test enforcement, not judging."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gate_suite(tmp_path: Path) -> Path:
    """Isolate the gate from this outer run, including any outer strict option."""
    root = tmp_path / "suite"
    tests = root / "tests"
    tests.mkdir(parents=True)
    for name in ("conftest.py", "test_integration.py"):
        shutil.copyfile(_ROOT / "tests" / name, tests / name)
    deploy = root / "deploy"
    deploy.mkdir()
    shutil.copyfile(_ROOT / "deploy" / "spot-user-data.sh", deploy / "spot-user-data.sh")
    (root / "pytest.ini").write_text("[pytest]\naddopts = -q --strict-markers\n")
    (root / "empty-path").mkdir()
    return root


def _engine(root: Path, fault: str = "none") -> Path:
    binary = root / "gate-fixture-engine"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, zipfile\n"
        "from pathlib import Path\n"
        f"fault = {fault!r}\n"
        f"events = Path({str(root / 'events')!r})\n"
        "args = sys.argv[1:]\n"
        "with events.open('a') as log: log.write(json.dumps(args) + '\\n')\n"
        "if args == ['--version']:\n"
        "    if fault == 'empty-version': sys.exit(0)\n"
        "    print('not an engine' if fault == 'bad-version' else 'cxxprobe 0.0.0')\n"
        "    sys.exit(2 if fault == 'version-failure' else 0)\n"
        "if args[:2] == ['new', 'contest']:\n"
        "    if fault == 'contest': sys.exit('broken contest sentinel')\n"
        "    contest = Path('worker-it')\n"
        "    contest.mkdir()\n"
        "    (contest / 'contest.yaml').write_text('version: 1\\nname: Worker IT\\n')\n"
        "elif args[:2] == ['package', 'init']:\n"
        "    if fault == 'init': sys.exit('broken init sentinel')\n"
        "    problem = Path('sum-two')\n"
        "    (problem / 'solutions').mkdir(parents=True)\n"
        "    (problem / 'tests').mkdir()\n"
        "    (problem / 'problem.yaml').write_text('version: 2\\nname: Sum Two\\n')\n"
        "elif args[:2] == ['package', 'pack']:\n"
        "    if fault == 'pack': sys.exit('broken pack sentinel')\n"
        "    with zipfile.ZipFile('p.zip', 'w') as archive:\n"
        "        archive.write('sum-two/problem.yaml', 'sum-two/problem.yaml')\n"
        "elif args[0] == 'judge':\n"
        "    if fault == 'sandbox':\n"
        "        print('cgroup unavailable sentinel', file=sys.stderr)\n"
        "        sys.exit(2)\n"
        "    submission = Path(args[args.index('--submission') + 1]).read_text()\n"
        "    wrong = 'a+b+1' in submission\n"
        "    out = Path(args[args.index('--output') + 1])\n"
        "    report = {'slug': 'sum-two', 'overall': 'FAIL' if wrong else 'PASS'}\n"
        "    out.write_text(json.dumps(report))\n"
        "    sys.exit(1 if wrong else 0)\n"
        "else:\n"
        "    sys.exit('unexpected fixture command')\n"
    )
    binary.chmod(0o755)
    return binary


def _run_gate(
    root: Path,
    binary: Path | None,
    *,
    strict: bool = True,
    args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for key in ("CXXPROBE_BINARY", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST"):
        env.pop(key, None)
    env.update(
        PATH=str(root / "empty-path"),
        PYTHONPATH=str(_ROOT / "src"),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    if binary is not None:
        env["CXXPROBE_BINARY"] = str(binary)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_integration.py",
            *(("--require-real-engine",) if strict else ()),
            *args,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    ("fault", "diagnostic"),
    [
        ("missing", "binary is missing or not executable"),
        ("nonexecutable", "binary is missing or not executable"),
        ("bad-version", "invalid real engine --version"),
        ("empty-version", "invalid real engine --version"),
        ("version-failure", "invalid real engine --version"),
        ("contest", "broken contest sentinel"),
        ("init", "broken init sentinel"),
        ("pack", "broken pack sentinel"),
        ("sandbox", "cgroup unavailable sentinel"),
    ],
)
def test_required_mode_rejects_unavailable_engine_or_pipeline(gate_suite, fault, diagnostic):
    binary = None if fault == "missing" else _engine(gate_suite, fault)
    if binary is not None and fault == "nonexecutable":
        binary.chmod(0o644)
    result = _run_gate(gate_suite, binary)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert diagnostic in output
    if fault in {"contest", "init", "pack", "sandbox"}:
        events = (gate_suite / "events").read_text()
        expected = {"contest": '"new"', "init": '"init"', "pack": '"pack"', "sandbox": '"judge"'}
        assert expected[fault] in events


@pytest.mark.parametrize("mark", ["skip", "skipif(True, reason='skip sentinel')", "xfail"])
def test_required_mode_rejects_marked_integration_cases(gate_suite, mark):
    path = gate_suite / "tests" / "test_integration.py"
    with path.open("a") as module:
        module.write(f"\npytestmark = pytest.mark.{mark}\n")
    result = _run_gate(gate_suite, _engine(gate_suite))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "rejects skipped/xfail integration" in result.stdout + result.stderr


@pytest.mark.parametrize("outcome", ["skip", "xfail"])
def test_required_mode_rejects_teardown_skip_after_all_calls_pass(gate_suite, outcome):
    with (gate_suite / "tests" / "conftest.py").open("a") as conftest:
        conftest.write(
            '\n@pytest.fixture(scope="session", autouse=True)\n'
            "def late_outcome():\n"
            "    yield\n"
            f"    pytest.{outcome}('teardown sentinel')\n"
        )
    result = _run_gate(gate_suite, _engine(gate_suite))
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "4 passed" in output, output
    assert "rejects skipped/xfail integration" in output
    assert "teardown sentinel" in output


@pytest.mark.parametrize("args", [("--collect-only",), ("-k", "correct_solution")])
def test_required_mode_rejects_cases_that_do_not_run(gate_suite, args):
    result = _run_gate(gate_suite, _engine(gate_suite), args=args)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "--require-real-engine" in result.stdout + result.stderr


def test_required_mode_rejects_empty_integration_module(gate_suite):
    (gate_suite / "tests" / "test_integration.py").write_text("")
    result = _run_gate(gate_suite, _engine(gate_suite))
    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        "all four real-engine integration cases must be selected" in result.stdout + result.stderr
    )


def test_gate_accepts_all_four_executed_calls(gate_suite):
    result = _run_gate(gate_suite, _engine(gate_suite))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout


@pytest.mark.parametrize("fault", ["missing", "contest", "init", "pack", "sandbox"])
def test_optional_developer_mode_keeps_environment_skips(gate_suite, fault):
    binary = None if fault == "missing" else _engine(gate_suite, fault)
    result = _run_gate(gate_suite, binary, strict=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped" in result.stdout
