"""Tests for algodesk doctor CLI command."""

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from algodesk.api.checks import CheckResult, DoctorCheck, Severity
from algodesk.cli.app import app


def test_doctor_summary_line_and_exit_code(runner: CliRunner) -> None:
    """doctor outputs summary line as first line and exits code 0 on healthy workspace."""
    from algodesk.ops.paths import find_project_root

    root = find_project_root()
    result = runner.invoke(app, ["--root", str(root), "doctor"])
    lines = result.output.strip().splitlines()
    assert lines[0].startswith("DOCTOR:")
    assert "passed" in lines[0]
    assert "warning" in lines[0]
    assert "0 critical" in lines[0]
    assert result.exit_code == 0


def test_doctor_verbose_output(runner: CliRunner) -> None:
    """doctor --verbose lists PASS lines and execution timings."""
    from algodesk.ops.paths import find_project_root

    root = find_project_root()
    result = runner.invoke(app, ["--root", str(root), "doctor", "--verbose"])
    assert "PASS python.version (" in result.output
    assert "ms):" in result.output


def test_doctor_only_flag(runner: CliRunner, make_project: Any) -> None:
    """doctor --only executes only the requested check ID."""
    proj = make_project("doc_only")
    result = runner.invoke(
        app, ["--root", str(proj), "doctor", "--only", "python.version", "--verbose"]
    )
    assert result.exit_code == 0
    assert "python.version" in result.output
    assert "paths.ready" not in result.output


def test_doctor_json_output(runner: CliRunner, make_project: Any) -> None:
    """--json doctor outputs machine-readable JSON parseable by json.loads."""
    proj = make_project("doc_json")
    result = runner.invoke(app, ["--json", "--root", str(proj), "doctor"])
    data = json.loads(result.output)
    assert "summary" in data
    assert data["summary"].startswith("DOCTOR:")
    assert "counts" in data
    assert "passed" in data["counts"]
    assert "warning" in data["counts"]
    assert "critical" in data["counts"]
    assert "results" in data
    assert isinstance(data["results"], list)


def test_doctor_warning_exits_code_one(
    runner: CliRunner, make_project: Any, monkeypatch: Any
) -> None:
    """Doctor with a warning check exits code 1."""
    import sys

    app_mod = sys.modules["algodesk.cli.app"]
    proj = make_project("doc_warn")

    def mock_checks(rt: Any) -> list[DoctorCheck]:
        return [
            DoctorCheck(
                id="custom.warn",
                severity=Severity.WARNING,
                run=lambda: CheckResult(ok=False, message="minor warning", fix="check it"),
            )
        ]

    monkeypatch.setattr(app_mod, "build_core_checks", mock_checks)

    result = runner.invoke(app, ["--root", str(proj), "doctor"])
    assert result.exit_code == 1
    assert "FAIL [warning] custom.warn: minor warning" in result.output
    assert "Fix: check it" in result.output


def test_doctor_critical_failure_exits_code_two(
    runner: CliRunner, make_project: Any, monkeypatch: Any
) -> None:
    """Doctor with a critical check failure exits code 2."""
    import sys

    app_mod = sys.modules["algodesk.cli.app"]
    proj = make_project("doc_crit")

    def mock_checks(rt: Any) -> list[DoctorCheck]:
        return [
            DoctorCheck(
                id="custom.crit",
                severity=Severity.CRITICAL,
                run=lambda: CheckResult(ok=False, message="severe failure", fix="resolve it"),
            )
        ]

    monkeypatch.setattr(app_mod, "build_core_checks", mock_checks)

    result = runner.invoke(app, ["--root", str(proj), "doctor"])
    assert result.exit_code == 2
    assert "FAIL [critical] custom.crit: severe failure" in result.output
    assert "Fix: resolve it" in result.output


def test_doctor_all_passing_exits_code_zero(
    runner: CliRunner, make_project: Any, monkeypatch: Any
) -> None:
    """Doctor with all passing checks exits code 0."""
    import sys

    app_mod = sys.modules["algodesk.cli.app"]
    proj = make_project("doc_pass")

    def mock_checks(rt: Any) -> list[DoctorCheck]:
        return [
            DoctorCheck(
                id="custom.pass",
                severity=Severity.CRITICAL,
                run=lambda: CheckResult(ok=True, message="everything ok"),
            )
        ]

    monkeypatch.setattr(app_mod, "build_core_checks", mock_checks)

    result = runner.invoke(app, ["--root", str(proj), "doctor"])
    assert result.exit_code == 0
    assert "DOCTOR: 1 passed, 0 warning, 0 critical" in result.output


def test_doctor_creates_no_files_read_only(runner: CliRunner, make_project: Any) -> None:
    """Running doctor creates no files in the workspace (strictly read-only)."""
    proj = make_project("doc_readonly")
    before = set(proj.rglob("*"))
    res = runner.invoke(app, ["--root", str(proj), "doctor"])
    assert res.exit_code in (0, 1, 2)
    after = set(proj.rglob("*"))
    assert before == after
