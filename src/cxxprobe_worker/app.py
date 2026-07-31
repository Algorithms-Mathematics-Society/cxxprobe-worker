"""Composition root: turns a config into a wired-up Worker.

Every dependency is constructed here and injected, so no module below this
one reaches for global state — which is what makes the whole thing testable
with fakes rather than a live filesystem and a real cxxprobe binary.
"""

from __future__ import annotations

from dataclasses import dataclass

from cxxprobe_worker.config import WorkerConfig
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
    )
    storage = build_storage(config.storage.backend, config.storage.root)
    workspaces = WorkspaceManager(
        config.workspace.root,
        keep_on_failure=config.workspace.keep_on_failure,
    )
    executor = JobExecutor(config.judge, workspaces, storage, logger)
    worker = Worker(config, queue, executor, logger, metrics, health)

    return Application(
        config=config,
        logger=logger,
        metrics=metrics,
        health=health,
        queue=queue,
        storage=storage,
        workspaces=workspaces,
        executor=executor,
        worker=worker,
    )
