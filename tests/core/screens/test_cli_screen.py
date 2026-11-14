"""Tests for screen CLI commands (screen list, screen show, screen run)."""

from __future__ import annotations

from typer.testing import CliRunner

from algodesk.cli.app import app

runner = CliRunner()


def test_cli_screen_list() -> None:
    res = runner.invoke(app, ["screen", "list"])
    assert res.exit_code == 0
    assert "NAME" in res.stdout
    assert "sma_trend_test" in res.stdout


def test_cli_screen_list_shows_sma_trend_test() -> None:
    res = runner.invoke(app, ["screen", "list"])
    assert res.exit_code == 0
    assert "sma_trend_test" in res.stdout


def test_cli_screen_show_sma_trend_test() -> None:
    res = runner.invoke(app, ["screen", "show", "sma_trend_test"])
    assert res.exit_code == 0
    assert "Name:" in res.stdout
    assert "sma_trend_test" in res.stdout
    assert "Requirements" in res.stdout
    assert "sma" in res.stdout


def test_cli_screen_show_missing_lists_available() -> None:
    res = runner.invoke(app, ["screen", "show", "nonexistent_screen_xyz"])
    assert res.exit_code != 0
    assert "nonexistent_screen_xyz" in res.stdout or "nonexistent_screen_xyz" in str(res.exception)
    assert "Available screens:" in (res.stdout + str(res.exception))
    assert "sma_trend_test" in (res.stdout + str(res.exception))


def test_cli_screen_run_missing_screen_lists_available() -> None:
    res = runner.invoke(app, ["screen", "run", "missing_screen_123", "--symbols", "RELIANCE"])
    assert res.exit_code != 0
    out = res.stdout + (str(res.exception) if res.exception else "")
    assert "Available screens:" in out
    assert "sma_trend_test" in out


def test_cli_screen_run_missing_group_lists_available() -> None:
    res = runner.invoke(
        app, ["screen", "run", "sma_trend_test", "--group", "nonexistent_group_xyz"]
    )
    assert res.exit_code != 0
    out = res.stdout + (str(res.exception) if res.exception else "")
    assert "Available groups:" in out
    assert "demo5" in out


def test_cli_screen_run_no_universe_specified_fails() -> None:
    res = runner.invoke(app, ["screen", "run", "sma_trend_test"])
    assert res.exit_code != 0
    out = res.stdout + (str(res.exception) if res.exception else "")
    assert "universe" in out.lower() or "--group" in out.lower()
