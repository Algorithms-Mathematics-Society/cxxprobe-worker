"""Report translation and delivery guarantees."""

from __future__ import annotations

from cxxprobe_worker.config import ControlPlaneConfig
from cxxprobe_worker.control_plane import ControlPlaneClient, summarise, worst_verdict
from cxxprobe_worker.jobs import JobResult, JobStatus


def test_worst_verdict_follows_cxxprobe_precedence():
    assert worst_verdict(["AC", "AC"]) == "AC"
    assert worst_verdict(["AC", "WA"]) == "WA"
    assert worst_verdict(["WA", "TLE"]) == "TLE"
    assert worst_verdict(["AC", "RE", "WA"]) == "RE"
    assert worst_verdict([]) == "SE"


def test_all_cases_accepted_summarises_to_ac():
    report = {
        "tests": {
            "manual": {
                "passed": 2,
                "total": 2,
                "cases": [
                    {
                        "label": "1",
                        "verdict": "AC",
                        "wall_time_ms": 12,
                        "peak_memory_bytes": 2048 * 1024,
                    },
                    {
                        "label": "2",
                        "verdict": "AC",
                        "wall_time_ms": 30,
                        "peak_memory_bytes": 1024 * 1024,
                    },
                ],
            }
        },
        "compile": {"solution": {"ok": True}},
    }
    out = summarise(report)
    assert out["verdict"] == "AC"
    assert out["passed_count"] == 2
    assert out["max_runtime_ms"] == 30
    assert out["max_memory_kb"] == 2048
    assert len(out["testcases"]) == 2
    assert out["testcases"][0]["testcase_no"] == 1


def test_one_wrong_case_summarises_to_wa():
    report = {
        "tests": {
            "manual": {
                "passed": 1,
                "total": 2,
                "cases": [{"verdict": "AC"}, {"verdict": "WA"}],
            }
        },
        "compile": {"solution": {"ok": True}},
    }
    assert summarise(report)["verdict"] == "WA"


def test_a_failed_solution_compile_is_ce_not_a_lost_job():
    """CE is a real verdict — the pipeline worked, the code didn't."""
    report = {
        "compile": {"solution": {"ok": False, "exit_code": 1, "diagnostics": "expected ';'"}},
        "tests": {"manual": {"passed": 0, "total": 0, "cases": []}},
    }
    out = summarise(report)
    assert out["verdict"] == "CE"
    assert "expected ';'" in out["compile_output"]
    assert out["testcases"] == []


def test_no_report_is_a_system_error():
    out = summarise(None, error="worker exploded")
    assert out["verdict"] == "SE"
    assert out["error"] == "worker exploded"


def test_checker_diagnostics_are_carried_through():
    report = {
        "compile": {"solution": {"ok": True}},
        "tests": {
            "manual": {
                "passed": 0,
                "total": 1,
                "cases": [
                    {"verdict": "WA", "checker_diagnostics": "wrong on token 3"},
                ],
            }
        },
    }
    assert summarise(report)["testcases"][0]["checker_message"] == "wrong on token 3"


# ── delivery ──────────────────────────────────────────────────────────────


def make_client(base_url: str = "") -> ControlPlaneClient:
    import io

    from cxxprobe_worker.monitoring import build_logger

    return ControlPlaneClient(
        ControlPlaneConfig(base_url=base_url),
        build_logger(name="cp-test", log_format="text", stream=io.StringIO()),
    )


def test_standalone_mode_reports_success_without_a_server():
    """No base_url means judge-and-store-only; nothing should be attempted."""
    client = make_client("")
    assert client.enabled is False
    assert client.register("host") is None
    result = JobResult(job_id="j1", status=JobStatus.SUCCEEDED)
    assert client.publish_result(result) is True


def test_a_retryable_job_reports_nothing():
    """It produced no verdict and will come back; there is nothing to say."""
    client = make_client("http://example.invalid")
    result = JobResult(job_id="j1", status=JobStatus.RETRYABLE, error="sandbox down")
    assert client.publish_result(result) is True


def test_undeliverable_result_returns_false_so_the_message_survives():
    """A judged submission whose verdict is lost is worse than one judged
    twice — nothing would ever notice the first."""
    client = make_client("http://127.0.0.1:9")  # nothing listens on port 9
    result = JobResult(
        job_id="j1",
        status=JobStatus.SUCCEEDED,
        report={"compile": {"solution": {"ok": True}}, "tests": {"manual": {"cases": []}}},
    )
    assert client.publish_result(result) is False
