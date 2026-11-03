"""Tests for algodesk calendar CLI commands."""

from __future__ import annotations

from typer.testing import CliRunner

from algodesk.cli.app import app


def test_calendar_show_mcx_evening_only(runner: CliRunner) -> None:
    res = runner.invoke(
        app,
        [
            "calendar",
            "show",
            "--exchange",
            "mcx",
            "--segment",
            "com",
            "--date",
            "2026-10-20",
        ],
    )
    assert res.exit_code == 0
    assert "evening" in res.output
    assert "morning" not in res.output


def test_calendar_show_mcx_two_sessions(runner: CliRunner) -> None:
    res = runner.invoke(
        app,
        [
            "calendar",
            "show",
            "--exchange",
            "mcx",
            "--segment",
            "com",
            "--date",
            "2026-10-22",
        ],
    )
    assert res.exit_code == 0
    assert "morning" in res.output
    assert "evening" in res.output


def test_calendar_show_closed(runner: CliRunner) -> None:
    # 2026-10-25 is a Sunday
    res = runner.invoke(
        app,
        [
            "calendar",
            "show",
            "--exchange",
            "mcx",
            "--segment",
            "com",
            "--date",
            "2026-10-25",
        ],
    )
    assert res.exit_code == 0
    assert "Market closed" in res.output


def test_calendar_holidays_nse(runner: CliRunner) -> None:
    res = runner.invoke(
        app,
        ["calendar", "holidays", "--exchange", "nse"],
    )
    assert res.exit_code == 0
    assert "16 holiday(s) found for NSE" in res.output
    assert "Republic Day" in res.output


def test_calendar_check_success_and_failure(runner: CliRunner) -> None:
    # 3 days check should succeed (assuming current date in early Nov 2026 before special session)
    res_ok = runner.invoke(
        app,
        ["calendar", "check", "--exchange", "nse", "--days", "3"],
    )
    assert res_ok.exit_code == 0
    assert "OK:" in res_ok.output

    # 30 days check includes 2026-11-08 with pending special session timings -> exit code 1
    res_fail = runner.invoke(
        app,
        ["calendar", "check", "--exchange", "nse", "--days", "30"],
    )
    assert res_fail.exit_code == 1
    assert "Calendar check failed for NSE" in res_fail.output
    assert "Pending special session without timings" in res_fail.output
