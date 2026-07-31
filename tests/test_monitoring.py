from __future__ import annotations

import io
import json
from pathlib import Path

from cxxprobe_worker.monitoring import HealthReporter, Metrics, build_logger


def test_json_logger_emits_one_object_per_event():
    stream = io.StringIO()
    log = build_logger(name="t1", level="info", log_format="json", stream=stream)
    log.info("job.start", job_id="j1", problem="a-warmup")

    payload = json.loads(stream.getvalue().strip())
    assert payload["event"] == "job.start"
    assert payload["level"] == "info"
    assert payload["job_id"] == "j1"
    assert "ts" in payload


def test_text_logger_renders_fields_inline():
    stream = io.StringIO()
    log = build_logger(name="t2", level="debug", log_format="text", stream=stream)
    log.warning("job.slow", job_id="j1")
    out = stream.getvalue()
    assert "job.slow" in out
    assert "job_id=j1" in out


def test_level_filtering_suppresses_lower_levels():
    stream = io.StringIO()
    log = build_logger(name="t3", level="warning", log_format="text", stream=stream)
    log.debug("noisy")
    log.info("also-noisy")
    log.error("important")
    out = stream.getvalue()
    assert "important" in out
    assert "noisy" not in out


def test_rebuilding_a_logger_does_not_duplicate_output():
    stream = io.StringIO()
    build_logger(name="t4", level="info", log_format="text", stream=stream)
    log = build_logger(name="t4", level="info", log_format="text", stream=stream)
    log.info("once")
    assert stream.getvalue().count("once") == 1


def test_metrics_count_and_snapshot():
    m = Metrics()
    m.incr("jobs_claimed")
    m.incr("jobs_claimed")
    m.incr("jobs_failed", 3)
    assert m.get("jobs_claimed") == 2
    assert m.snapshot() == {"jobs_claimed": 2, "jobs_failed": 3}


def test_metrics_of_an_unseen_counter_is_zero():
    assert Metrics().get("never_touched") == 0


def test_health_reporter_is_a_noop_without_a_path():
    reporter = HealthReporter(None, "w1", Metrics())
    assert reporter.enabled is False
    reporter.write("running")  # must not raise


def test_health_file_is_written_with_state_and_counters(tmp_path: Path):
    metrics = Metrics()
    metrics.incr("jobs_succeeded", 2)
    path = tmp_path / "nested" / "health.json"
    reporter = HealthReporter(path, "w1", metrics)

    reporter.write("running")
    payload = json.loads(path.read_text())
    assert payload["worker_id"] == "w1"
    assert payload["state"] == "running"
    assert payload["counters"]["jobs_succeeded"] == 2
    assert "uptime_seconds" in payload


def test_health_file_is_rewritten_on_each_call(tmp_path: Path):
    path = tmp_path / "health.json"
    reporter = HealthReporter(path, "w1", Metrics())
    reporter.write("running")
    reporter.write("idle")
    assert json.loads(path.read_text())["state"] == "idle"


def test_health_write_failure_does_not_raise(tmp_path: Path):
    # A failed heartbeat must never take the worker down — a stale file is
    # the probe's problem to notice, not a reason to stop judging.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    reporter = HealthReporter(blocker / "health.json", "w1", Metrics())
    reporter.write("running")
