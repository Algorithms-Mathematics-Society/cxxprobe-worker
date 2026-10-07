"""Report translation and delivery guarantees."""

from __future__ import annotations

import json
from pathlib import Path

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


# ── the three test families ───────────────────────────────────────────────

FIXTURES = Path(__file__).parent / "fixtures"


def test_a_behaviour_only_problem_is_not_reported_as_a_system_error():
    """The bug this section exists for.

    b-pokemon-raii has an empty tests/ by design — the GTest cases *are* the
    tests. Reading only `tests.manual` found no verdicts and fell through to
    SE, so a submission that passed 6/6 was shown to the contestant as the
    judge having broken.
    """
    report = json.loads((FIXTURES / "behaviour_pass.json").read_text())
    result = summarise(report)

    assert result["verdict"] == "AC"
    assert result["total_count"] == 6
    assert result["passed_count"] == 6
    assert all(c["kind"] == "behavior" for c in result["testcases"])


def test_behaviour_cases_carry_the_gtest_name():
    """A setter debugging a failure needs to know *which* assertion broke,
    not just that something did."""
    report = json.loads((FIXTURES / "behaviour_pass.json").read_text())
    labels = [c["label"] for c in summarise(report)["testcases"]]

    assert any("PokemonRAII." in label for label in labels)


def test_a_failing_behaviour_case_is_wa_and_keeps_its_message():
    report = {
        "overall": "FAIL",
        "tests": {
            "behavior": {
                "status": "FAIL",
                "cases": [
                    {"name": "Suite.Holds", "failed": False, "time_ms": 2},
                    {
                        "name": "Suite.Releases",
                        "failed": True,
                        "time_ms": 3,
                        "failure_messages": ["expected 0 live allocations, saw 1"],
                    },
                ],
            }
        },
    }
    result = summarise(report)

    assert result["verdict"] == "WA"
    assert result["passed_count"] == 1
    assert result["total_count"] == 2
    failing = next(c for c in result["testcases"] if c["verdict"] == "WA")
    assert "live allocations" in failing["checker_message"]


def test_a_violated_symbolic_rule_is_a_wrong_answer():
    """a-beet-cast rejects memcpy through these. A rule that only produced
    prose would let a submission that broke the point of the problem pass."""
    report = {
        "overall": "FAIL",
        "tests": {
            "manual": {"cases": [{"label": "1", "verdict": "AC", "wall_time_ms": 4}]},
            "symbolic": {
                "status": "FAIL",
                "checks": [
                    {"kind": "must_include", "pattern": "std::bit_cast", "satisfied": True},
                    {
                        "kind": "must_not_include",
                        "pattern": "memcpy",
                        "satisfied": False,
                        "message": "Use std::bit_cast for type punning instead of memcpy.",
                    },
                ],
            },
        },
    }
    result = summarise(report)

    assert result["verdict"] == "WA", "passing the I/O tests is not enough on its own"
    violated = [c for c in result["testcases"] if c["kind"] == "symbolic" and c["verdict"] == "WA"]
    assert len(violated) == 1
    assert "bit_cast" in violated[0]["checker_message"]


def test_all_three_families_appear_in_one_submission():
    report = {
        "overall": "PASS",
        "tests": {
            "manual": {"cases": [{"label": "1", "verdict": "AC", "wall_time_ms": 5}]},
            "behavior": {"cases": [{"name": "S.T", "failed": False, "time_ms": 1}]},
            "symbolic": {"checks": [{"kind": "must_include", "pattern": "x", "satisfied": True}]},
        },
    }
    result = summarise(report)

    assert [c["kind"] for c in result["testcases"]] == ["io", "behavior", "symbolic"]
    # Numbering is continuous across families so the UI can order them.
    assert [c["testcase_no"] for c in result["testcases"]] == [1, 2, 3]
    assert result["verdict"] == "AC"


def test_a_report_with_no_checks_at_all_trusts_cxxprobes_own_overall():
    """A problem may legitimately have nothing to run. PASS means the judge
    was satisfied; ERROR means it could not do its job."""
    assert summarise({"overall": "PASS", "tests": {}})["verdict"] == "AC"
    assert summarise({"overall": "ERROR", "tests": {}})["verdict"] == "SE"


def test_a_crashed_test_section_is_a_runtime_error_not_a_pass():
    """A GTest binary that segfaults reports `behavior: ERROR` with zero
    cases. The other families may have passed outright — reporting AC on the
    strength of those would pass a submission whose code brought the run
    down."""
    report = {
        "overall": "ERROR",
        "compile": {"solution": {"ok": True}, "behavior_binary": {"ok": True}},
        "tests": {
            "manual": {"status": "PASS", "cases": [{"label": "1", "verdict": "AC"}]},
            "symbolic": {"status": "PASS", "checks": []},
            "behavior": {"status": "ERROR", "cases": []},
        },
    }
    result = summarise(report)

    assert result["verdict"] == "RE"
    assert result["passed_count"] == 1, "the checks that did run still count"


# ── custom runs ──────────────────────────────────────────────────────────
#
# `judge --custom-cases` emits a different report shape: {status, compile,
# cases} with no `tests` section, because the problem's own three checks
# never ran. The ordinary path found no manual/behavior/symbolic section,
# collected no verdicts and called the whole thing System Error -- a run
# that worked perfectly, reported as a platform failure.

CUSTOM_REPORT = {
    "status": "FAIL",
    "compile": {"solution": {"ok": True, "exit_code": 0}},
    "cases": [
        {
            "label": "my case",
            "ran": True,
            "verdict": "AC",
            "stdout": "12\n",
            "stderr": "read ok\n",
            "exit_code": 0,
            "cpu_time_ms": 2,
            "wall_time_ms": 4,
            "peak_memory_bytes": 524288,
        },
        {
            "label": "wrong on purpose",
            "ran": True,
            "verdict": "WA",
            "stdout": "12\n",
            "stderr": "",
            "exit_code": 0,
            "wall_time_ms": 3,
            "peak_memory_bytes": 528384,
        },
        {
            "label": "no expected",
            "ran": True,
            "stdout": "101\n",
            "stderr": "",
            "exit_code": 0,
            "wall_time_ms": 2,
            "peak_memory_bytes": 339968,
        },
    ],
}


def test_a_custom_run_is_not_a_system_error():
    """The bug this replaces. No `tests` section meant no verdicts, and the
    fallback called a perfectly good run SE."""
    out = summarise(CUSTOM_REPORT)
    assert out["verdict"] != "SE"
    assert out["verdict"] == "WA", "the worst judged case drives it"


def test_the_programs_output_survives_the_translation():
    # The whole point of a custom run: the candidate wants to see this.
    out = summarise(CUSTOM_REPORT)
    assert [c["stdout"] for c in out["testcases"]] == ["12\n", "12\n", "101\n"]
    assert out["testcases"][0]["stderr"] == "read ok\n"


def test_an_unjudged_case_has_no_verdict_and_is_not_counted():
    # No expected output was given, so there is nothing to be wrong about.
    # Counting it would turn "show me what this prints" into a failure.
    out = summarise(CUSTOM_REPORT)
    assert out["testcases"][2]["verdict"] == ""
    assert out["total_count"] == 2
    assert out["passed_count"] == 1


def test_a_run_with_nothing_judged_is_not_a_failure():
    report = {
        "status": "PASS",
        "compile": {"solution": {"ok": True}},
        "cases": [{"label": "Case 1", "ran": True, "stdout": "hi\n", "stderr": ""}],
    }
    out = summarise(report)
    assert out["verdict"] == "AC"
    assert out["total_count"] == 0
    assert out["testcases"][0]["stdout"] == "hi\n"


def test_a_custom_run_that_will_not_compile_still_reports_ce():
    # The compile branch must win over the custom branch, or the candidate
    # gets an empty case list instead of their compiler errors.
    report = {
        "status": "ERROR",
        "compile": {"solution": {"ok": False, "diagnostics": "error: expected ';'"}},
        "cases": [],
    }
    out = summarise(report)
    assert out["verdict"] == "CE"
    assert "expected ';'" in out["compile_output"]


def test_an_ordinary_report_is_untouched_by_the_custom_branch():
    # The dispatch keys on the absence of `tests`. A normal report must not
    # fall into it whatever else it carries.
    out = summarise(
        {
            "overall": "PASS",
            "compile": {"solution": {"ok": True}},
            "tests": {"manual": {"status": "PASS", "cases": [{"label": "1", "verdict": "AC"}]}},
        }
    )
    assert out["verdict"] == "AC"
    assert all("stdout" not in c for c in out["testcases"])
