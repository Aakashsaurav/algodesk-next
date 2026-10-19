"""Tests for CLI basic commands, root discovery, global options, and error formatting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from algodesk.cli.app import app


def test_cli_version(runner: CliRunner) -> None:
    """version command prints algodesk version."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "algodesk 0.0.0" in result.output


def test_cli_help(runner: CliRunner) -> None:
    """--help displays usage and available subcommands."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage: algodesk" in result.output
    for cmd in ["version", "doctor", "param", "plugins", "journal", "config", "new"]:
        assert cmd in result.output


def test_cli_subcommand_helps(runner: CliRunner) -> None:
    """Each sub-app displays help information."""
    for sub in ["param", "plugins", "journal", "config", "new"]:
        result = runner.invoke(app, [sub, "--help"])
        assert result.exit_code == 0
        assert f"Usage: algodesk {sub}" in result.output


def test_cli_unknown_option_exits_code_two(runner: CliRunner) -> None:
    """Passing an unrecognized option is a usage error that exits code 2."""
    result = runner.invoke(app, ["--invalid-flag"])
    assert result.exit_code == 2


def test_cli_root_option_explicit(runner: CliRunner, make_project: Any) -> None:
    """--root correctly targets the specified workspace directory."""
    proj = make_project("explicit_root")
    result = runner.invoke(app, ["--root", str(proj), "config", "show"])
    assert result.exit_code == 0
    assert "EFFECTIVE CONFIG" in result.output


def test_cli_invalid_root_error_formatting(runner: CliRunner, tmp_path: Path) -> None:
    """Invalid workspace root raises ConfigError, formatting three error lines and exiting 1."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    result = runner.invoke(app, ["--root", str(empty_dir), "doctor"])
    assert result.exit_code == 1
    assert "ERROR: Project root not found" in result.output
    assert f"WHERE: {empty_dir.resolve()}" in result.output
    assert "FIX: Run from within an AlgoDesk project or specify --root" in result.output


def test_cli_bug_formatting_and_exit_code_70(
    runner: CliRunner, make_project: Any, monkeypatch: Any
) -> None:
    """Unhandled internal exception prints bug lines and exits 70."""
    import sys

    app_mod = sys.modules["algodesk.cli.app"]
    proj = make_project("bug_proj")

    def broken_check(*args: Any, **kwargs: Any) -> Any:
        raise ZeroDivisionError("division by zero simulation")

    monkeypatch.setattr(app_mod, "build_core_checks", broken_check)

    result = runner.invoke(app, ["--root", str(proj), "doctor"])
    assert result.exit_code == 70
    assert "INTERNAL ERROR (bug): ZeroDivisionError: division by zero simulation" in result.output
    assert "run with --debug for the traceback" in result.output


def test_cli_debug_flag_shows_traceback(
    runner: CliRunner, make_project: Any, monkeypatch: Any
) -> None:
    """--debug flag prints Python traceback for internal bugs."""
    import sys

    app_mod = sys.modules["algodesk.cli.app"]
    proj = make_project("debug_proj")

    def broken_check(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("simulated value error")

    monkeypatch.setattr(app_mod, "build_core_checks", broken_check)

    result = runner.invoke(app, ["--debug", "--root", str(proj), "doctor"])
    assert result.exit_code == 70
    assert "Traceback (most recent call last):" in result.output
    assert "simulated value error" in result.output


def test_cli_main_entrypoint(monkeypatch: Any) -> None:
    """cli.main() top-level function runs and intercepts exceptions."""
    import sys

    import pytest

    from algodesk.cli import main

    monkeypatch.setattr(sys, "argv", ["algodesk", "version"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
