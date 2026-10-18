"""Tests for algodesk.api.checks — DoctorCheck, CheckResult, Severity."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from algodesk.api.checks import CheckResult, DoctorCheck, Severity


def test_severity_values() -> None:
    """Severity must define critical and warning strings."""
    assert Severity.CRITICAL == "critical"
    assert Severity.WARNING == "warning"
    assert Severity("critical") is Severity.CRITICAL
    assert Severity("warning") is Severity.WARNING


def test_check_result_success() -> None:
    """CheckResult with ok=True works with or without fix."""
    res_default = CheckResult(ok=True, message="Component healthy")
    assert res_default.ok is True
    assert res_default.message == "Component healthy"
    assert res_default.fix is None

    res_explicit = CheckResult(ok=True, message="Component healthy", fix=None)
    assert res_explicit.fix is None


def test_check_result_failure_without_fix_allowed() -> None:
    """CheckResult with ok=False and no fix is explicitly allowed."""
    res = CheckResult(ok=False, message="Service unreachable")
    assert res.ok is False
    assert res.message == "Service unreachable"
    assert res.fix is None

    res_none = CheckResult(ok=False, message="Service unreachable", fix=None)
    assert res_none.ok is False
    assert res_none.fix is None


def test_check_result_failure_with_fix() -> None:
    """CheckResult with ok=False and a fix instruction."""
    res = CheckResult(ok=False, message="Port 8000 in use", fix="Kill running process")
    assert res.ok is False
    assert res.message == "Port 8000 in use"
    assert res.fix == "Kill running process"


def test_check_result_is_frozen() -> None:
    """CheckResult is immutable and cannot be modified."""
    res = CheckResult(ok=True, message="ok")
    with pytest.raises(FrozenInstanceError):
        res.ok = False  # type: ignore[misc]


def test_doctor_check_execution() -> None:
    """DoctorCheck runs its callable and returns CheckResult."""
    calls: list[CheckResult] = []

    def runner() -> CheckResult:
        res = CheckResult(ok=True, message="ready")
        calls.append(res)
        return res

    check = DoctorCheck(id="broker.ping", severity=Severity.CRITICAL, run=runner)
    assert check.id == "broker.ping"
    assert check.severity is Severity.CRITICAL
    result = check.run()
    assert len(calls) == 1
    assert result.ok is True
    assert result.message == "ready"


def test_doctor_check_is_frozen() -> None:
    """DoctorCheck is immutable and cannot be modified."""
    check = DoctorCheck(
        id="sys.memory",
        severity=Severity.WARNING,
        run=lambda: CheckResult(ok=True, message="ok"),
    )
    with pytest.raises(FrozenInstanceError):
        check.id = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        check.severity = Severity.CRITICAL  # type: ignore[misc]
