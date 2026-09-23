"""Composition root: turns a config into a wired-up Worker.

Every dependency is constructed here and injected, so no module below this
one reaches for global state — which is what makes the whole thing testable
with fakes rather than a live filesystem and a real cxxprobe binary.
"""

from __future__ import annotations

from dataclasses import dataclass

from cxxprobe_worker.config import WorkerConfig
from cxxprobe_worker.control_plane import ControlPlaneClient
from cxxprobe_worker.daemon import Worker
from cxxprobe_worker.executor import JobExecutor
from cxxprobe_worker.monitoring import HealthReporter, Logger, Metrics, build_logger
from cxxprobe_worker.queue import IJobQueue, build_queue
from cxxprobe_worker.storage import IArtifactStorage, build_storage
from cxxprobe_worker.workspace import WorkspaceManager


@dataclass
class Application:
    config: WorkerConfig
    logger: Logger
    metrics: Metrics
    health: HealthReporter
    queue: IJobQueue
    storage: IArtifactStorage
    workspaces: WorkspaceManager
    executor: JobExecutor
    control_plane: ControlPlaneClient
    worker: Worker


def build_application(config: WorkerConfig) -> Application:
    logger = build_logger(
        level=config.monitoring.log_level,
        log_format=config.monitoring.log_format,
    )
    metrics = Metrics()
    health = HealthReporter(config.monitoring.health_file, config.worker_id, metrics)

    queue = build_queue(
        config.queue.backend,
        config.queue.root,
        config.queue.visibility_timeout_seconds,
        config.queue.max_delivery_attempts,
        queue_url=config.queue.queue_url,
        region=config.aws.region,
        wait_time_seconds=config.queue.wait_time_seconds,
        secondary_queue_url=config.queue.secondary_queue_url,
    )
    storage = build_storage(
        config.storage.backend,
        config.storage.root,
        bucket=config.storage.bucket,
        prefix=config.storage.prefix,
        region=config.aws.region,
    )
    workspaces = WorkspaceManager(
        config.workspace.root,
        keep_on_failure=config.workspace.keep_on_failure,
    )
    # An S3 fetcher only exists when something in the deployment can hand
    # out s3:// URIs; a purely local worker never needs boto3 at all.
    fetcher = None
    if config.storage.backend == "s3" or config.queue.backend == "sqs":
        from cxxprobe_worker.aws.fetch import S3Fetcher

        fetcher = S3Fetcher(region=config.aws.region)

    # Packages are worth keeping between jobs exactly when they come over
    # the network. A local worker reads them off its own disk already.
    packages = None
    if fetcher is not None:
        from cxxprobe_worker.packages import PackageCache

        packages = PackageCache(
            root=config.workspace.root.parent / "packages",
            ttl_seconds=config.judge.package_cache_seconds,
        )
        packages.prune()

    executor = JobExecutor(
        config.judge, workspaces, storage, logger, fetcher=fetcher, packages=packages
    )

    control_plane = ControlPlaneClient(config.control_plane, logger)
    worker = Worker(config, queue, executor, logger, metrics, health, control_plane=control_plane)

    return Application(
        config=config,
        logger=logger,
        metrics=metrics,
        health=health,
        queue=queue,
        storage=storage,
        workspaces=workspaces,
        executor=executor,
        control_plane=control_plane,
        worker=worker,
    )
