"""Reading a ``cxxprobe test project --json`` report.

The shape is cxxprobe's (cli/commands/test_project_cmd.cpp and
src/judge/integration.cpp), and the tests pin it with reports captured from
the real engine in tests/fixtures/project:

* ``parts``: a list, one entry per declared label, in manifest order. Each
  has ``label``, ``slug``, ``origin`` (``submission`` | ``reference`` |
  ``empty``), ``source``, ``status``, and ``report``, an ordinary judge
  report, absent when the part was ``empty`` and nothing ran.
* ``integration``: ``status``, ``cases[]`` (each with ``parts``, the labels
  its name carries), ``parts[]`` (what was linked for each label, with
  ``compiled`` and the part's own compile ``diagnostics`` when it failed),
  ``link`` (the final build step; absent when it never ran) and ``error``.
* ``substitutions``: ``label``, ``origin`` and ``reason`` for every part that
  was not the submitter's own code.

Only this module knows those keys, so the executor and the AMS summary cannot
drift apart from each other the way they drifted from the engine.
"""

from __future__ import annotations

from typing import Any

from cxxprobe_worker.jobs import PART_LABEL


def is_project_report(report: dict[str, Any] | None) -> bool:
    return (
        isinstance(report, dict)
        and isinstance(report.get("parts"), list)
        and isinstance(report.get("integration"), dict)
    )


def part_entries(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Each part entry with a well-formed label, in report order.

    Labels become storage keys and summary keys, and they come from the
    report rather than the validated job, so anything else is dropped.
    """
    parts = report.get("parts")
    if not isinstance(parts, list):
        return []
    return [
        part
        for part in parts
        if isinstance(part, dict)
        and isinstance(part.get("label"), str)
        and PART_LABEL.fullmatch(part["label"])
    ]


def part_reports(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Label to that part's own judge report, for parts that were judged."""
    return {
        part["label"]: part["report"]
        for part in part_entries(report)
        if isinstance(part.get("report"), dict)
    }


def integration(report: dict[str, Any]) -> dict[str, Any]:
    block = report.get("integration")
    return block if isinstance(block, dict) else {}


def integration_link(report: dict[str, Any]) -> dict[str, Any] | None:
    link = integration(report).get("link")
    return link if isinstance(link, dict) else None


def integration_compile_failures(report: dict[str, Any]) -> dict[str, str]:
    """Label to diagnostics for each part whose source would not build for
    the integration binary (and had no usable fallback)."""
    out: dict[str, str] = {}
    for part in integration(report).get("parts") or []:
        if not isinstance(part, dict) or part.get("compiled") is not False:
            continue
        label = part.get("label")
        if isinstance(label, str) and PART_LABEL.fullmatch(label):
            out[label] = str(part.get("diagnostics") or "")
    return out


def link_rejected(report: dict[str, Any]) -> bool:
    """The linker ran and refused the parts: the submitters' fault.

    A negative exit code means the sandbox never launched it, which is the
    machine's, exactly as for a single submission's compile step.
    """
    link = integration_link(report)
    if link is None or link.get("ok") is not False:
        return False
    exit_code = link.get("exit_code")
    return isinstance(exit_code, int) and exit_code >= 0


def suite_crashed(report: dict[str, Any]) -> bool:
    """Everything linked, then the integration binary died without a report.

    Code that links and then crashes is the submitted code's doing. A suite
    that could not even be started is not, and cxxprobe words that
    differently ("could not start").
    """
    block = integration(report)
    link = integration_link(report)
    return (
        str(block.get("status") or "").upper() == "ERROR"
        and link is not None
        and link.get("ok") is True
        and "without a test report" in str(block.get("error") or "")
    )
