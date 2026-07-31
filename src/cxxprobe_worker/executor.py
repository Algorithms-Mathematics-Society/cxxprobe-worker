"""Runs one job by shelling out to ``cxxprobe judge``.

This module is the whole of the worker's judging knowledge, and it is
deliberately thin. It does not know what a verdict is, how a checker works,
or what a problem package contains. It knows one command:

    cxxprobe judge (--package ZIP | --problem-dir DIR)
                   --submission FILE --output REPORT.json

and one contract:

    exit 0  → judged, everything passed
    exit 1  → judged, something failed (a real WA/TLE verdict)
    exit 2  → could not judge (bad config, missing file, unusable sandbox)

The **exit code**, not the presence of a report, decides whether judging
happened. cxxprobe writes a report on exit 2 as well — on a host without a
usable sandbox it emits one whose `overall` is ERROR — so treating "a report
exists" as success would record a verdict for a submission that never ran.

Exit 1 is a *successful* job: the submission was wrong, which is a normal
outcome and must never be retried. Exit 2 is retryable, because the most
common causes (no cgroup delegation, a full disk, a half-deployed host) are
properties of the machine rather than of the job.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

from cxxprobe_worker.config import JudgeConfig
from cxxprobe_worker.jobs import Job, JobResult, JobStatus
from cxxprobe_worker.monitoring import Logger
from cxxprobe_worker.storage import IArtifactStorage, StorageError
from cxxprobe_worker.workspace import Workspace, WorkspaceManager

# `cxxprobe judge` exit codes. 0 and 1 both mean "judging happened".
JUDGED_EXIT_CODES = frozenset({0, 1})


class PreparationError(RuntimeError):
    """The job's inputs could not be staged into the workspace.

    Always a permanent failure: a missing or corrupt package will still be
    missing or corrupt on the next attempt.
    """


def _diagnostics_from(report: dict[str, Any]) -> str:
    """Pull the most specific failure text out of a judge report.

    When cxxprobe cannot judge it still writes a report, and the useful
    detail is buried in whichever compile step failed — surfacing it beats
    logging a bare exit code.
    """
    compile_section = report.get("compile")
    if not isinstance(compile_section, dict):
        return ""
    for step in compile_section.values():
        if isinstance(step, dict) and not step.get("ok", True):
            text = step.get("diagnostics")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _is_zip(path: Path) -> bool:
    return path.is_file() and zipfile.is_zipfile(path)


def _materialise(uri: str, destination: Path, fetcher: Any) -> Path:
    """Bring a possibly-remote job input onto local disk.

    Local paths pass straight through; ``s3://`` URIs are downloaded. This is
    the only place that distinction exists, so everything downstream can
    assume the inputs are files.
    """
    from cxxprobe_worker.aws.fetch import FetchError, is_remote

    if not is_remote(uri):
        return Path(uri)
    if fetcher is None:
        raise PreparationError(f"job references {uri} but no S3 fetcher is configured")
    try:
        fetched: Path = fetcher.fetch(str(uri), destination)
    except FetchError as exc:
        raise PreparationError(str(exc)) from exc
    return fetched


def _stage_inputs(job: Job, workspace: Workspace, fetcher: Any = None) -> tuple[str, Path]:
    """Bring the job's package and submission into its private workspace.

    Returns the ``cxxprobe judge`` flag to use for the package and its path.
    Copying (or downloading) rather than referencing in place means a job can
    never mutate shared state, and a retry starts from identical inputs.
    """
    from cxxprobe_worker.aws.fetch import is_remote

    submission_src = _materialise(job.submission_path, workspace.submission_path, fetcher)
    if submission_src != workspace.submission_path:
        if not submission_src.is_file():
            raise PreparationError(f"submission not found: {submission_src}")
        try:
            shutil.copy2(submission_src, workspace.submission_path)
        except OSError as exc:
            raise PreparationError(f"cannot stage submission: {exc}") from exc

    if is_remote(job.package_path):
        # A remote package is always a .cxxpkg zip; cxxprobe unpacks it.
        staged = _materialise(job.package_path, workspace.root / "package.zip", fetcher)
        return "--package", staged

    package = Path(job.package_path)
    if _is_zip(package):
        staged = workspace.root / "package.zip"
        try:
            shutil.copy2(package, staged)
        except OSError as exc:
            raise PreparationError(f"cannot stage package zip: {exc}") from exc
        return "--package", staged

    if package.is_dir():
        staged = workspace.package_dir
        try:
            shutil.copytree(package, staged, dirs_exist_ok=True)
        except OSError as exc:
            raise PreparationError(f"cannot stage package directory: {exc}") from exc
        return "--problem-dir", staged

    raise PreparationError(f"package is neither a zip nor a directory: {package}")


class JobExecutor:
    """Executes jobs. Stateless between calls apart from its collaborators."""

    def __init__(
        self,
        judge: JudgeConfig,
        workspaces: WorkspaceManager,
        storage: IArtifactStorage,
        logger: Logger,
        fetcher: Any = None,
    ) -> None:
        self._judge = judge
        self._workspaces = workspaces
        self._storage = storage
        self._log = logger
        # Only set when the deployment can receive s3:// job inputs.
        self._fetcher = fetcher

    def execute(self, job: Job) -> JobResult:
        """Run one job to completion. Never raises for a job-level failure."""
        started = time.monotonic()
        self._log.info("job.start", job_id=job.job_id, problem=job.problem_slug)

        try:
            with self._workspaces.session(job.job_id) as workspace:
                result = self._run_in_workspace(job, workspace, started)
        except PreparationError as exc:
            result = JobResult(
                job_id=job.job_id,
                status=JobStatus.FAILED,
                duration_seconds=time.monotonic() - started,
                error=str(exc),
            )
        except OSError as exc:
            # Workspace creation failed — disk full, permissions. Another
            # worker, or this one after cleanup, may well succeed.
            result = JobResult(
                job_id=job.job_id,
                status=JobStatus.RETRYABLE,
                duration_seconds=time.monotonic() - started,
                error=f"workspace unavailable: {exc}",
            )

        self._log.info(
            "job.finish",
            job_id=job.job_id,
            status=result.status.value,
            exit_code=result.exit_code,
            duration_seconds=round(result.duration_seconds, 3),
            error=result.error,
        )
        return result

    def _run_in_workspace(self, job: Job, workspace: Workspace, started: float) -> JobResult:
        package_flag, package_path = _stage_inputs(job, workspace, self._fetcher)

        argv = [
            self._judge.binary,
            "judge",
            package_flag,
            str(package_path),
            "--submission",
            str(workspace.submission_path),
            "--output",
            str(workspace.report_path),
            *self._judge.extra_args,
        ]
        self._log.debug("judge.invoke", job_id=job.job_id, argv=argv)

        try:
            # argv is built here from config and job paths — never shell-parsed.
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._judge.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return JobResult(
                job_id=job.job_id,
                status=JobStatus.RETRYABLE,
                duration_seconds=time.monotonic() - started,
                error=f"cxxprobe binary not found: {self._judge.binary}",
            )
        except subprocess.TimeoutExpired:
            return JobResult(
                job_id=job.job_id,
                status=JobStatus.RETRYABLE,
                duration_seconds=time.monotonic() - started,
                error=f"cxxprobe judge exceeded {self._judge.timeout_seconds}s",
            )

        return self._interpret(job, workspace, completed, started)

    def _interpret(
        self,
        job: Job,
        workspace: Workspace,
        completed: subprocess.CompletedProcess[str],
        started: float,
    ) -> JobResult:
        duration = time.monotonic() - started
        report = self._read_report(workspace)

        # The exit code — not the presence of a report — decides whether
        # judging happened. cxxprobe exits 2 *and still writes a report* when
        # it could not judge (a broken sandbox, for instance, yields a report
        # whose overall is ERROR). Trusting the report there would record a
        # verdict for a submission that was never actually run.
        if completed.returncode not in JUDGED_EXIT_CODES:
            detail = completed.stderr.strip() or completed.stdout.strip() or "(no output)"
            if report is not None:
                detail = _diagnostics_from(report) or detail
            result = JobResult(
                job_id=job.job_id,
                status=JobStatus.RETRYABLE,
                exit_code=completed.returncode,
                duration_seconds=duration,
                report=report,
                error=f"cxxprobe judge could not judge (exit {completed.returncode}): {detail}",
            )
        elif report is None:
            # Exit 0/1 means judging happened, so a missing report is
            # cxxprobe contradicting its own contract — retrying can't help.
            result = JobResult(
                job_id=job.job_id,
                status=JobStatus.FAILED,
                exit_code=completed.returncode,
                duration_seconds=duration,
                error=(
                    f"cxxprobe judge exited {completed.returncode} without writing a report: "
                    f"{completed.stderr.strip() or completed.stdout.strip() or '(no output)'}"
                ),
            )
        else:
            result = JobResult(
                job_id=job.job_id,
                status=JobStatus.SUCCEEDED,
                exit_code=completed.returncode,
                duration_seconds=duration,
                report=report,
            )

        result.artifacts = self._persist(job, workspace, completed, result)
        return result

    def _read_report(self, workspace: Workspace) -> dict[str, object] | None:
        if not workspace.report_path.is_file():
            return None
        try:
            parsed = json.loads(workspace.report_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            self._log.warning("report.unreadable", path=str(workspace.report_path), error=str(exc))
            return None
        if not isinstance(parsed, dict):
            self._log.warning("report.not_an_object", path=str(workspace.report_path))
            return None
        return parsed

    def _persist(
        self,
        job: Job,
        workspace: Workspace,
        completed: subprocess.CompletedProcess[str],
        result: JobResult,
    ) -> dict[str, str]:
        """Store the report and the judge's own output.

        Artifact storage failing must not turn a judged submission into a
        failed job — the verdict is already known and is the valuable part.
        Log and carry on.
        """
        artifacts: dict[str, str] = {}
        try:
            if result.report is not None:
                artifacts["report.json"] = self._storage.put_text(
                    job.job_id, "report.json", json.dumps(result.report, indent=2)
                )
            if completed.stdout:
                artifacts["stdout.log"] = self._storage.put_text(
                    job.job_id, "stdout.log", completed.stdout
                )
            if completed.stderr:
                artifacts["stderr.log"] = self._storage.put_text(
                    job.job_id, "stderr.log", completed.stderr
                )
            if workspace.submission_path.is_file():
                artifacts["submission.cpp"] = self._storage.put_file(
                    job.job_id, "submission.cpp", workspace.submission_path
                )
        except StorageError as exc:
            self._log.error("artifact.store_failed", job_id=job.job_id, error=str(exc))
        return artifacts
