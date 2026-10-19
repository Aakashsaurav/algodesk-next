"""Tests for ops/doctor.py — DoctorRegistry, DoctorReport, formatting, and JSON output."""

from __future__ import annotations

import json

from algodesk.api.checks import CheckResult, DoctorCheck, Severity
from algodesk.ops.doctor import DoctorRegistry, format_report, report_to_json


def test_doctor_all_passing_exit_code_zero() -> None:
    """All checks passing produces exit_code 0 and correct summary."""
    reg = DoctorRegistry()
    reg.register(
        DoctorCheck(
            id="check.one",
            severity=Severity.CRITICAL,
            run=lambda: CheckResult(ok=True, message="check one ok"),
        )
    )
    reg.register(
        DoctorCheck(
            id="check.two",
            severity=Severity.WARNING,
            run=lambda: CheckResult(ok=True, message="check two ok"),
        )
    )

    report = reg.run()
    assert report.exit_code == 0
    assert report.passed == 2
    assert report.warning == 0
    assert report.critical == 0

    formatted = format_report(report)
    assert formatted.splitlines()[0] == "DOCTOR: 2 passed, 0 warning, 0 critical"


def test_doctor_warning_only_exit_code_one() -> None:
    """Warning-level failure without critical failure produces exit_code 1."""
    reg = DoctorRegistry()
    reg.register(
        DoctorCheck(
            id="check.pass",
            severity=Severity.CRITICAL,
            run=lambda: CheckResult(ok=True, message="pass"),
        )
    )
    reg.register(
        DoctorCheck(
            id="check.warn",
            severity=Severity.WARNING,
            run=lambda: CheckResult(ok=False, message="low resource", fix="clean up"),
        )
    )

    report = reg.run()
    assert report.exit_code == 1
    assert report.passed == 1
    assert report.warning == 1
    assert report.critical == 0

    formatted = format_report(report)
    assert formatted.splitlines()[0] == "DOCTOR: 1 passed, 1 warning, 0 critical"
    assert "FAIL [warning] check.warn: low resource" in formatted
    assert "Fix: clean up" in formatted


def test_doctor_critical_failure_exit_code_two() -> None:
    """Critical-level failure produces exit_code 2."""
    reg = DoctorRegistry()
    reg.register(
        DoctorCheck(
            id="check.crit",
            severity=Severity.CRITICAL,
            run=lambda: CheckResult(ok=False, message="security flaw", fix="patch system"),
        )
    )
    reg.register(
        DoctorCheck(
            id="check.warn",
            severity=Severity.WARNING,
            run=lambda: CheckResult(ok=False, message="low disk", fix="clean space"),
        )
    )

    report = reg.run()
    assert report.exit_code == 2
    assert report.passed == 0
    assert report.warning == 1
    assert report.critical == 1

    formatted = format_report(report)
    assert formatted.splitlines()[0] == "DOCTOR: 0 passed, 1 warning, 1 critical"
    assert "FAIL [critical] check.crit: security flaw" in formatted


def test_doctor_crashing_check_fails_closed_as_critical() -> None:
    """A check that raises an unhandled exception becomes a critical failure; run never raises."""
    reg = DoctorRegistry()

    def crashing_check() -> CheckResult:
        raise RuntimeError("database connection timed out")

    reg.register(
        DoctorCheck(
            id="crash.test",
            severity=Severity.WARNING,  # Even if declared warning, crash is critical
            run=crashing_check,
        )
    )

    report = reg.run()
    assert report.exit_code == 2
    assert report.critical == 1
    assert report.passed == 0
    assert len(report.results) == 1

    res = report.results[0]
    assert res.ok is False
    assert res.severity == Severity.CRITICAL
    assert "check crashed: RuntimeError: database connection timed out" in res.message


def test_doctor_only_filter() -> None:
    """--only ID executes only the specified check."""
    reg = DoctorRegistry()
    reg.register(
        DoctorCheck(
            id="first",
            severity=Severity.CRITICAL,
            run=lambda: CheckResult(ok=True, message="first ok"),
        )
    )
    reg.register(
        DoctorCheck(
            id="second",
            severity=Severity.CRITICAL,
            run=lambda: CheckResult(ok=True, message="second ok"),
        )
    )

    report = reg.run(only="second")
    assert len(report.results) == 1
    assert report.results[0].id == "second"


def test_doctor_verbose_formatting() -> None:
    """Verbose format includes passing checks, timings, and fixes."""
    reg = DoctorRegistry()
    reg.register(
        DoctorCheck(
            id="fast.pass",
            severity=Severity.CRITICAL,
            run=lambda: CheckResult(ok=True, message="all good"),
        )
    )
    reg.register(
        DoctorCheck(
            id="slow.fail",
            severity=Severity.WARNING,
            run=lambda: CheckResult(ok=False, message="needs attention", fix="fix me"),
        )
    )

    report = reg.run()
    verbose_out = format_report(report, verbose=True)

    assert "PASS fast.pass (" in verbose_out
    assert "FAIL [warning] slow.fail (" in verbose_out
    assert "Fix: fix me" in verbose_out


def test_doctor_report_to_json() -> None:
    """report_to_json generates parseable JSON with summary, counts, exit_code, and results."""
    reg = DoctorRegistry()
    reg.register(
        DoctorCheck(
            id="json.check",
            severity=Severity.CRITICAL,
            run=lambda: CheckResult(ok=True, message="healthy"),
        )
    )

    report = reg.run()
    data = report_to_json(report)
    serialized = json.dumps(data)
    loaded = json.loads(serialized)

    assert loaded["summary"] == "DOCTOR: 1 passed, 0 warning, 0 critical"
    assert loaded["exit_code"] == 0
    assert loaded["counts"]["passed"] == 1
    assert len(loaded["results"]) == 1
    assert loaded["results"][0]["id"] == "json.check"
    assert loaded["results"][0]["ok"] is True
