"""Hardening tests for run, replay, results, and telegram CLI commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from algodesk.cli.app import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# algodesk run command hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "non_existent_run",
        "missing_config_file",
        "corrupt_run_name",
        "unknown_profile_run",
        "invalid_run_xyz",
    ],
)
def test_cli_run_invalid_run_names(bad_name: str) -> None:
    res = runner.invoke(app, ["run", bad_name, "--dry-run"])
    assert res.exit_code != 0


def test_cli_run_dry_run_observe_mode() -> None:
    res = runner.invoke(app, ["run", "demo_paper", "--dry-run", "--observe"])
    assert res.exit_code == 0
    assert "Dry-Run Execution Plan" in res.output
    assert "demo_paper" in res.output


# ---------------------------------------------------------------------------
# algodesk replay command hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_date",
    [
        "invalid_date",
        "2026/10/26",
        "26-10-2026",
        "2026-13-45",
        "2026-00-10",
        "not-a-date",
        "2026-10-32",
    ],
)
def test_cli_replay_invalid_date_formats(bad_date: str) -> None:
    res = runner.invoke(app, ["replay", "--dates", bad_date, "--run", "demo_paper"])
    assert res.exit_code != 0


@pytest.mark.parametrize(
    "bad_run",
    [
        "non_existent_run_cfg",
        "unknown_replay_run",
        "missing_run_file",
    ],
)
def test_cli_replay_invalid_run_config(bad_run: str) -> None:
    res = runner.invoke(app, ["replay", "--dates", "2026-10-26", "--run", bad_run])
    assert res.exit_code != 0


@pytest.mark.parametrize(
    "dates_arg",
    [
        "2026-10-26",
        "2026-10-26,2026-10-26",
    ],
)
def test_cli_replay_dates_parameter_execution(dates_arg: str) -> None:
    res = runner.invoke(
        app,
        ["replay", "--dates", dates_arg, "--run", "demo_paper"],
    )
    assert res.exit_code == 0
    assert "results folder:" in res.output


def test_cli_replay_twice_reproducibility() -> None:
    res = runner.invoke(
        app,
        [
            "replay",
            "--dates",
            "2026-10-26",
            "--run",
            "demo_paper",
            "--twice",
        ],
    )
    assert res.exit_code == 0
    assert "replay equal: True" in res.output


# ---------------------------------------------------------------------------
# algodesk results command hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nonexistent_id",
    [
        "run_never_existed_123",
        "bad_results_folder_name",
        "non_existent_id",
        "dummy_run_id_456",
    ],
)
def test_cli_results_show_missing_run_id(nonexistent_id: str) -> None:
    res = runner.invoke(app, ["results", "show", nonexistent_id])
    assert res.exit_code != 0


def test_cli_results_list_contains_table_headers() -> None:
    res = runner.invoke(app, ["results", "list"])
    assert res.exit_code == 0
    assert "RUN ID" in res.output
    assert "MODE" in res.output
    assert "TRADES" in res.output
    assert "NET P&L" in res.output


# ---------------------------------------------------------------------------
# algodesk telegram command hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "error_phrase"),
    [
        (400, "Bad Request"),
        (401, "Unauthorized"),
        (403, "Forbidden"),
        (404, "Not Found"),
        (500, "Internal Server Error"),
        (502, "Bad Gateway"),
        (503, "Service Unavailable"),
    ],
)
def test_cli_telegram_test_http_errors(status_code: int, error_phrase: str) -> None:
    mock_resp = httpx.Response(
        status_code=status_code,
        request=httpx.Request("POST", "https://api.telegram.org/bot123/sendMessage"),
        text=f'{{"ok": false, "description": "{error_phrase}"}}',
    )
    mock_prov = MagicMock()
    mock_prov.get.side_effect = lambda k: "123456:ABC-DEF" if "token" in k else "987654321"

    with (
        patch("algodesk.cli.telegram._resolve_secrets_provider", return_value=mock_prov),
        patch("httpx.Client.post", return_value=mock_resp),
    ):
        res = runner.invoke(app, ["telegram", "test"])
        assert res.exit_code != 0
        assert "Telegram API error" in res.output


def test_cli_telegram_test_network_timeout() -> None:
    mock_prov = MagicMock()
    mock_prov.get.side_effect = lambda k: "123456:ABC-DEF" if "token" in k else "987654321"

    with (
        patch("algodesk.cli.telegram._resolve_secrets_provider", return_value=mock_prov),
        patch("httpx.Client.post", side_effect=httpx.ConnectTimeout("Connection timed out")),
    ):
        res = runner.invoke(app, ["telegram", "test"])
        assert res.exit_code != 0
        assert "Failed to send test message" in res.output


def test_cli_telegram_test_success() -> None:
    mock_resp = httpx.Response(
        status_code=200,
        request=httpx.Request("POST", "https://api.telegram.org/bot123/sendMessage"),
        text='{"ok": true, "result": {"message_id": 100}}',
    )
    mock_prov = MagicMock()
    mock_prov.get.side_effect = lambda k: "123456:ABC-DEF" if "token" in k else "987654321"

    with (
        patch("algodesk.cli.telegram._resolve_secrets_provider", return_value=mock_prov),
        patch("httpx.Client.post", return_value=mock_resp),
    ):
        res = runner.invoke(app, ["telegram", "test"])
        assert res.exit_code == 0
        assert "OK: sent test message to chat 987654321" in res.output
