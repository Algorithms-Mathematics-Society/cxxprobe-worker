"""Structured logging, counters, and a heartbeat file.

Logs are JSON by default because the worker's output is read by a log
aggregator far more often than by a human at a terminal; ``text`` exists for
local development, where the opposite is true.

Metrics are process-local counters written into the heartbeat file rather
than exposed over HTTP. The worker has no inbound network surface by design
— it polls a queue and writes files — and adding a listener purely for
metrics would be the only reason it ever needed one.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{time.strftime('%H:%M:%S', time.gmtime(record.created))} "
            f"{record.levelname.lower():<7} {record.getMessage()}"
        )
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict) and extra:
            rendered = " ".join(f"{k}={v}" for k, v in extra.items() if v is not None)
            if rendered:
                return f"{base}  {rendered}"
        return base


class Logger:
    """Thin structured wrapper over ``logging``.

    Events are short dotted names (``job.start``) with structured fields,
    rather than prose — so a query like ``event:job.finish AND status:failed``
    works without regex over free text.
    """

    def __init__(self, inner: logging.Logger) -> None:
        self._inner = inner

    def _emit(self, level: int, event: str, **fields: Any) -> None:
        self._inner.log(level, event, extra={"fields": fields})

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, **fields)


def build_logger(
    name: str = "cxxprobe-worker",
    level: str = "info",
    log_format: str = "json",
    stream: Any = None,
) -> Logger:
    inner = logging.getLogger(name)
    inner.setLevel(LEVELS.get(level, logging.INFO))
    inner.propagate = False
    # Rebuild handlers so repeated calls (tests, re-config) don't duplicate output.
    for handler in list(inner.handlers):
        inner.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(_JsonFormatter() if log_format == "json" else _TextFormatter())
    inner.addHandler(handler)
    return Logger(inner)


@dataclass
class Metrics:
    """Process-local counters. Safe to update from worker threads."""

    started_at: float = field(default_factory=time.monotonic)
    _counts: Counter[str] = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] += amount

    def get(self, name: str) -> int:
        with self._lock:
            return self._counts[name]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at


class HealthReporter:
    """Rewrites a small JSON heartbeat file so a liveness probe can read it.

    A probe should check both that the file exists and that its ``ts`` is
    recent — a worker wedged inside a job stops rewriting it, which existence
    alone would not reveal.
    """

    def __init__(self, path: Path | None, worker_id: str, metrics: Metrics) -> None:
        self._path = Path(path) if path else None
        self._worker_id = worker_id
        self._metrics = metrics

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def payload(self, state: str) -> dict[str, Any]:
        return {
            "worker_id": self._worker_id,
            "state": state,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "uptime_seconds": round(self._metrics.uptime_seconds, 1),
            "counters": self._metrics.snapshot(),
        }

    def write(self, state: str = "running") -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a probe never reads a half-written file.
            tmp = self._path.with_suffix(f"{self._path.suffix}.tmp")
            tmp.write_text(json.dumps(self.payload(state), indent=2))
            tmp.replace(self._path)
        except OSError:
            # A failed heartbeat must never take the worker down; the probe
            # noticing a stale file is the correct escalation path.
            pass
