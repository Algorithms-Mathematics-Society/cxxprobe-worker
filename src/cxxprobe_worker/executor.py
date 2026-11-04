"""Runs one job by shelling out to ``cxxprobe judge``.

This module is the whole of the worker's judging knowledge, and it is
deliberately thin. It does not know what a verdict is, how a checker works,
or what a problem package contains. It knows one command:

    cxxprobe judge (--package ZIP | --problem-dir DIR)
            [--custom-cases FILE]
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

# The `compile` steps in a cxxprobe report that build the submission itself.
SUBMISSION_COMPILE_STEPS = ("solution", "behavior_binary")


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


def _submission_failed_to_compile(report: dict[str, Any]) -> bool:
    """True when it was the *submission* that would not build.

    This is the difference between "this machine is broken" and "this code
    is broken". cxxprobe exits 2 for both and both report a failed solution
    compile, so shape alone cannot separate them — but the compile step's
    **exit code** can:

        exit_code >= 0   the compiler ran and rejected the source
        exit_code <  0   the sandbox never managed to launch it

    Only the first is the submitter's fault. Getting this backwards matters:
    a syntax error fails identically on every worker, and during a contest
    those are a large share of all submissions, so retrying them would
    starve the queue of real work.

    The submission is compiled by more than one step. On a problem judged
    only by GTest there is no `solution` step at all: the submission is
    built into `behavior_binary` together with the checker. Looking at
    `solution` alone took every compile error on such a problem for a broken
    machine, retried it until it dead-lettered, and left the submission
    "queued" for ever. That happened to a real Pokemon RAII run on 2026-10-06.
    `checker_binary` is deliberately not on the list: it doesn't contain the
    submission, and package verification has already built it against the
    reference solution.
    """
    compile_section = report.get("compile")
    if not isinstance(compile_section, dict):
        return False
    for name in SUBMISSION_COMPILE_STEPS:
        step = compile_section.get(name)
        if not isinstance(step, dict) or step.get("ok") is not False:
            continue
        exit_code = step.get("exit_code")
        if isinstance(exit_code, int) and exit_code >= 0:
            return True
    return False


def _submission_crashed_at_runtime(report: dict[str, Any]) -> bool:
    """True when everything built but the submission's own code brought a
    test run down.

    A GTest binary that segfaults produces `behavior: ERROR` with zero cases
    and cxxprobe exits 2 — the same exit code as "this host has no usable
    sandbox". The discriminator is the **compile** section: if every step
    built cleanly, the machine was working, and what failed afterwards was
    the submitted code.

    This matters more than it sounds. In a contest about RAII and manual
    memory, a use-after-free is among the most likely things a contestant
    writes; treating those as retryable meant each one was re-run until it
    dead-lettered, burning the queue and leaving the submission stuck at
    "queued" with no verdict ever recorded.
    """
    compile_section = report.get("compile")
    if not isinstance(compile_section, dict) or not compile_section:
        return False
    # Every step must have actually built. A step that never launched means
    # the sandbox, not the submission, is the problem.
    if not all(
        isinstance(step, dict) and step.get("ok") is True for step in compile_section.values()
    ):
        return False

    tests = report.get("tests")
    if not isinstance(tests, dict):
        return False
    return any(
        isinstance(section, dict) and str(section.get("status", "")).upper() == "ERROR"
        for section in tests.values()
    )


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


def _stage_file(uri: str, destination: Path, fetcher: Any, what: str) -> None:
    """Fetch or copy one source file to ``destination``."""
    source = _materialise(uri, destination, fetcher)
    if source == destination:
        return
    if not source.is_file():
        raise PreparationError(f"{what} not found: {source}")
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        raise PreparationError(f"cannot stage {what}: {exc}") from exc


def _stage_inputs(
    job: Job, workspace: Workspace, fetcher: Any = None, cache: Any = None
) -> tuple[str, Path]:
    """Bring the job's package and submission into its private workspace.

    Returns the ``cxxprobe judge`` flag to use for the package and its path.
    Copying (or downloading) rather than referencing in place means a job can
    never mutate shared state, and a retry starts from identical inputs.
    """
    if job.submission_path is None:
        raise PreparationError("problem job has no submission_path")
    _stage_file(job.submission_path, workspace.submission_path, fetcher, "submission")
    return _stage_package(job, workspace, fetcher, cache)


def _stage_parts(job: Job, workspace: Workspace, fetcher: Any = None) -> dict[str, Path]:
    """Bring every part of a project job into the one workspace, by label.

    Through the same fetch path as an ordinary submission, so a part can be
    a local file or an s3:// URI alike, and none of them is ever cached.
    """
    staged: dict[str, Path] = {}
    try:
        workspace.parts_dir.mkdir(exist_ok=True)
    except OSError as exc:
        raise PreparationError(f"cannot create parts directory: {exc}") from exc
    for label, uri in sorted((job.parts or {}).items()):
        destination = workspace.part_path(label)
        _stage_file(uri, destination, fetcher, f"submission for part {label}")
        staged[label] = destination
    return staged


def declared_part_labels(package: Path, project_slug: str) -> list[str] | None:
    """The part labels a package declares for ``project_slug``.

    Read from the pack's ``manifest.json`` (``projects[].parts[].label``),
    which exists so a consumer need not open the project itself to learn its
    shape. ``None`` when there is no readable declaration at all, which leaves
    the decision to cxxprobe; ``[]`` when the package declares projects but
    not this one.
    """
    try:
        if _is_zip(package):
            with zipfile.ZipFile(package) as archive:
                try:
                    raw = archive.read("manifest.json")
                except KeyError:
                    return None
        elif (package / "manifest.json").is_file():
            raw = (package / "manifest.json").read_bytes()
        else:
            return None
        manifest = json.loads(raw)
    except (OSError, zipfile.BadZipFile, ValueError):
        return None

    projects = manifest.get("projects") if isinstance(manifest, dict) else None
    if not isinstance(projects, list):
        return None
    for project in projects:
        if isinstance(project, dict) and project.get("slug") == project_slug:
            return [
                str(part["label"])
                for part in project.get("parts") or []
                if isinstance(part, dict) and part.get("label") is not None
            ]
    return []


def _check_parts_against_package(job: Job, package: Path) -> None:
    """Refuse a project job whose parts don't match what the package declares.

    Before any part is fetched. An unsupplied part is not harmless: cxxprobe
    fills it with its reference, so a team missing a part would be graded as
    though it had written one.
    """
    slug = job.project_slug or ""
    declared = declared_part_labels(package, slug)
    if declared is None:
        return
    if not declared:
        raise PreparationError(f"package declares no project {slug!r}")
    supplied = set(job.parts or {})
    unknown = sorted(supplied - set(declared))
    if unknown:
        raise PreparationError(f"project {slug!r} has no part labelled {', '.join(unknown)}")
    missing = [label for label in declared if label not in supplied]
    if missing:
        raise PreparationError(f"project {slug!r} has no submission for part {', '.join(missing)}")


def _stage_package(
    job: Job, workspace: Workspace, fetcher: Any = None, cache: Any = None
) -> tuple[str, Path]:
    """Bring the job's package into its workspace, through the cache when remote."""
    from cxxprobe_worker.aws.fetch import is_remote

    if is_remote(job.package_path):
        # A remote package is always a .cxxpkg zip; cxxprobe unpacks it.
        #
        # Fetched once and reused: in a contest this is one download per
        # *submission* for a handful of distinct packages, and on a fleet
        # spread across regions — which the account's quotas force — each of
        # those is a cross-region round trip costing more than the job. See
        # packages.py. Submissions are never cached; they are different
        # every time.
        staged = workspace.root / "package.zip"
        if cache is not None:
            cached = cache.fresh(job.package_path)
            if cached is not None:
                cache.hits += 1
                try:
                    shutil.copy2(cached, staged)
                except OSError as exc:
                    raise PreparationError(f"cannot stage cached package: {exc}") from exc
                return "--package", staged
            cache.misses += 1

        fetched = _materialise(job.package_path, staged, fetcher)
        if cache is not None:
            cache.store(job.package_path, fetched)
        return "--package", fetched

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
        packages: Any = None,
    ) -> None:
        self._judge = judge
        self._workspaces = workspaces
        self._storage = storage
        self._log = logger
        # Only set when the deployment can receive s3:// job inputs.
        self._fetcher = fetcher
        # Only set when packages are worth keeping between jobs, which is
        # whenever they come from S3. None disables caching entirely.
        self._packages = packages

    def execute(self, job: Job) -> JobResult:
        """Run one job to completion. Never raises for a job-level failure."""
        started = time.monotonic()
        self._log.info(
            "job.start",
            job_id=job.job_id,
            job_type=job.job_type,
            problem=job.problem_slug,
            project=job.project_slug,
        )

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

        cache = self._packages
        self._log.info(
            "job.finish",
            job_id=job.job_id,
            package_cached=(cache.hits if cache is not None else None),
            status=result.status.value,
            exit_code=result.exit_code,
            duration_seconds=round(result.duration_seconds, 3),
            error=result.error,
        )
        return result

    def _run_in_workspace(self, job: Job, workspace: Workspace, started: float) -> JobResult:
        if job.job_type == "project":
            return self._run_project(job, workspace, started)

        package_flag, package_path = _stage_inputs(job, workspace, self._fetcher, self._packages)

        argv = [
            self._judge.binary,
            "judge",
            package_flag,
            str(package_path),
            "--submission",
            str(workspace.submission_path),
            "--output",
            str(workspace.report_path),
        ]

        if job.custom_cases is not None:
            # An empty list is a client bug, and the dangerous reading of it
            # would be "no custom cases, judge normally" -- which runs the
            # real tests and returns a scored-looking verdict for something
            # the candidate asked to run against their own input. Refuse.
            if not job.custom_cases:
                return JobResult(
                    job_id=job.job_id,
                    status=JobStatus.FAILED,
                    duration_seconds=time.monotonic() - started,
                    error="custom_cases was empty; refusing to fall back to the problem's tests",
                )
            try:
                workspace.custom_cases_path.write_text(
                    json.dumps(job.custom_cases), encoding="utf-8"
                )
            except OSError as exc:
                return JobResult(
                    job_id=job.job_id,
                    status=JobStatus.RETRYABLE,
                    duration_seconds=time.monotonic() - started,
                    error=f"cannot write custom cases: {exc}",
                )
            argv += ["--custom-cases", str(workspace.custom_cases_path)]

        argv += self._judge.extra_args
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

    def _run_project(self, job: Job, workspace: Workspace, started: float) -> JobResult:
        """Stage a project job: its package, then every part by label."""
        _, package_path = _stage_package(job, workspace, self._fetcher, self._packages)
        _check_parts_against_package(job, package_path)
        staged = _stage_parts(job, workspace, self._fetcher)
        self._log.info(
            "project.staged", job_id=job.job_id, project=job.project_slug, parts=sorted(staged)
        )
        return JobResult(
            job_id=job.job_id,
            status=JobStatus.FAILED,
            duration_seconds=time.monotonic() - started,
            error="project jobs are staged but this worker cannot judge them yet",
        )

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

            # A submission that will not compile is the submitter's problem,
            # not the machine's. It fails identically on every worker, so
            # retrying it just burns the queue — and in a contest a large
            # share of submissions are exactly this.
            would_not_compile = report is not None and _submission_failed_to_compile(report)
            crashed = report is not None and _submission_crashed_at_runtime(report)
            submitter_at_fault = would_not_compile or crashed

            if would_not_compile:
                message = f"submission failed to compile: {detail}"
            elif crashed:
                # Named distinctly from a compile failure because the remedy
                # differs entirely: one is a syntax error, the other is
                # memory-unsafe code that killed the test binary.
                message = f"submission crashed during testing: {detail}"
            else:
                message = f"cxxprobe judge could not judge (exit {completed.returncode}): {detail}"

            result = JobResult(
                job_id=job.job_id,
                status=JobStatus.FAILED if submitter_at_fault else JobStatus.RETRYABLE,
                exit_code=completed.returncode,
                duration_seconds=duration,
                report=report,
                error=message,
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
