"""Command-line entry points."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from cxxprobe_worker.app import build_application
from cxxprobe_worker.config import ConfigError, WorkerConfig, load_config
from cxxprobe_worker.jobs import Job

app = typer.Typer(
    name="cxxprobe-worker",
    help="Job-execution daemon that runs cxxprobe judge jobs and stores their artifacts.",
    no_args_is_help=True,
    add_completion=False,
)

EnvOption = Annotated[str, typer.Option("--env", "-e", help="Environment name (test/staging/prod)")]
ConfigDirOption = Annotated[
    Path, typer.Option("--config-dir", "-c", help="Directory holding <env>.yaml")
]


def _load(environment: str, config_dir: Path) -> WorkerConfig:
    try:
        return load_config(environment, config_dir)
    except ConfigError as exc:
        typer.secho(f"cxxprobe-worker: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


@app.command()
def run(
    env: EnvOption = "test",
    config_dir: ConfigDirOption = Path("config"),
    max_jobs: Annotated[
        int | None, typer.Option("--max-jobs", help="Exit after this many jobs")
    ] = None,
) -> None:
    """Run the poll loop until stopped."""
    config = _load(env, config_dir)
    if max_jobs is not None:
        config = config.model_copy(update={"max_jobs": max_jobs})

    application = build_application(config)
    application.worker.install_signal_handlers()
    # A worker killed mid-job leaves its workspace behind and nothing else
    # will ever clean it up, so startup is the one safe moment to sweep.
    pruned = application.workspaces.prune()
    if pruned:
        application.logger.info("workspace.pruned", count=pruned)

    application.worker.run()


@app.command()
def once(
    env: EnvOption = "test",
    config_dir: ConfigDirOption = Path("config"),
) -> None:
    """Claim and run at most one job, then exit.

    Exit 0 if a job ran and succeeded, 1 if it failed, 3 if the queue was
    empty — so a cron-style caller can tell "nothing to do" from "something
    went wrong".
    """
    config = _load(env, config_dir)
    application = build_application(config)
    result = application.worker.run_once()

    if result is None:
        typer.echo("queue is empty")
        raise typer.Exit(3)

    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    raise typer.Exit(0 if result.ok else 1)


@app.command()
def submit(
    package: Annotated[Path, typer.Option("--package", help="Problem package zip or directory")],
    submission: Annotated[Path, typer.Option("--submission", help="Source file to grade")],
    job_id: Annotated[str | None, typer.Option("--job-id", help="Defaults to a random id")] = None,
    problem: Annotated[str | None, typer.Option("--problem", help="Advisory problem slug")] = None,
    env: EnvOption = "test",
    config_dir: ConfigDirOption = Path("config"),
) -> None:
    """Enqueue a job. Mostly for local testing and manual rejudges."""
    import uuid

    config = _load(env, config_dir)
    application = build_application(config)

    job = Job(
        job_id=job_id or f"job-{uuid.uuid4().hex[:12]}",
        package_path=str(package.resolve()),
        submission_path=str(submission.resolve()),
        problem_slug=problem,
    )
    application.queue.publish(job)
    typer.echo(f"queued {job.job_id}")


@app.command()
def status(
    env: EnvOption = "test",
    config_dir: ConfigDirOption = Path("config"),
) -> None:
    """Print queue depth and the last heartbeat, if there is one."""
    config = _load(env, config_dir)
    application = build_application(config)

    payload: dict[str, object] = {
        "environment": config.environment,
        "worker_id": config.worker_id,
        "queue_depth": application.queue.depth(),
    }
    health_file = config.monitoring.health_file
    if health_file and Path(health_file).is_file():
        try:
            payload["health"] = json.loads(Path(health_file).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            payload["health_error"] = str(exc)

    typer.echo(json.dumps(payload, indent=2, default=str))


@app.command()
def doctor(
    env: EnvOption = "test",
    config_dir: ConfigDirOption = Path("config"),
) -> None:
    """Check that the config loads and everything it points at is usable.

    Worth running before a deploy: it catches a missing cxxprobe binary or an
    unwritable artifact root at start-up rather than on the first real job.
    """
    config = _load(env, config_dir)
    problems: list[str] = []

    binary = config.judge.binary
    resolved = (
        shutil.which(binary) if "/" not in binary else (binary if Path(binary).is_file() else None)
    )
    if resolved is None:
        problems.append(f"cxxprobe binary not found: {binary}")
    else:
        typer.echo(f"  cxxprobe        {resolved}")
        try:
            version = subprocess.run(
                [resolved, "--version"], capture_output=True, text=True, timeout=10, check=False
            )
            typer.echo(f"  version         {version.stdout.strip() or '(no output)'}")
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(f"cannot run {resolved} --version: {exc}")

    for label, path in (
        ("workspace root", config.workspace.root),
        ("artifact root", config.storage.root),
        ("queue root", config.queue.root),
    ):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            probe = Path(path) / ".cxxprobe-worker-write-test"
            probe.write_text("ok")
            probe.unlink()
            typer.echo(f"  {label:<15} {path}")
        except OSError as exc:
            problems.append(f"{label} not writable ({path}): {exc}")

    if problems:
        typer.echo("")
        for problem in problems:
            typer.secho(f"  error  {problem}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    typer.secho("\nOK", fg=typer.colors.GREEN)


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(app())
