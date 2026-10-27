"""CLI tests for run, replay, results, and telegram commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from algodesk.cli.app import app

runner = CliRunner()


def test_cli_run_dry_run() -> None:
    """algodesk run demo_paper --dry-run prints execution plan and doctor status."""
    res = runner.invoke(app, ["run", "demo_paper", "--dry-run"])
    assert res.exit_code == 0
    assert "Dry-Run Execution Plan" in res.output
    assert "demo_paper" in res.output
    assert "0 critical, 0 warnings" in res.output
    assert "No network connections opened" in res.output


def test_cli_run_dry_run_unknown_config() -> None:
    """algodesk run non_existent --dry-run raises cleanly."""
    res = runner.invoke(app, ["run", "non_existent_run", "--dry-run"])
    assert res.exit_code != 0


def test_cli_replay_twice_command(tmp_path: Path) -> None:
    """algodesk replay --dates 2026-10-26 --run demo_paper --twice asserts replay equal."""
    res = runner.invoke(app, ["replay", "--dates", "2026-10-26", "--run", "demo_paper", "--twice"])
    assert res.exit_code == 0
    assert "replay equal: True" in res.output
    assert "results folder:" in res.output


def test_cli_replay_single_run() -> None:
    """algodesk replay --dates 2026-10-26 --run demo_paper executes single replay."""
    res = runner.invoke(app, ["replay", "--dates", "2026-10-26", "--run", "demo_paper"])
    assert res.exit_code == 0
    assert "results folder:" in res.output


def test_cli_results_list_and_show() -> None:
    """algodesk results list and show commands inspect generated results."""
    # First ensure a run exists
    replay_res = runner.invoke(app, ["replay", "--dates", "2026-10-26", "--run", "demo_paper"])
    assert replay_res.exit_code == 0
    run_dir = replay_res.output.split("results folder:")[-1].strip().splitlines()[0]
    run_id = Path(run_dir).name

    # Test results list
    list_res = runner.invoke(app, ["results", "list"])
    assert list_res.exit_code == 0
    assert "RUN ID" in list_res.output
    assert run_id in list_res.output

    # Test results show text
    show_res = runner.invoke(app, ["results", "show", run_id])
    assert show_res.exit_code == 0
    assert "Run Summary" in show_res.output

    # Test results show JSON
    json_res = runner.invoke(app, ["results", "show", run_id, "--json"])
    assert json_res.exit_code == 0
    data = json.loads(json_res.output)
    assert "gross_pnl" in data
    assert "net_pnl" in data


def test_cli_results_show_missing() -> None:
    """algodesk results show invalid_id raises ConfigError."""
    res = runner.invoke(app, ["results", "show", "non_existent_bundle_12345"])
    assert res.exit_code != 0


def test_cli_telegram_setup_and_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """algodesk telegram setup with explicit options stores secrets and can be tested."""
    fake_secrets: dict[str, str] = {}
    mock_prov = MagicMock()
    mock_prov.set.side_effect = lambda k, v: fake_secrets.update({k: v})
    mock_prov.get.side_effect = lambda k: fake_secrets[k]
    monkeypatch.setattr(
        "algodesk.cli.telegram._resolve_secrets_provider",
        lambda rt, **kw: mock_prov,
    )

    def mock_post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})

    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **kwargs: mock_post(url, **kwargs))

    setup_res = runner.invoke(
        app,
        ["telegram", "setup", "--token", "test_bot_token_xyz", "--chat-id", "987654321"],
    )
    assert setup_res.exit_code == 0
    assert "OK: configured bot token and chat_id 987654321" in setup_res.output
    assert fake_secrets["telegram_bot_token"] == "test_bot_token_xyz"
    assert fake_secrets["telegram_chat_id"] == "987654321"

    test_res = runner.invoke(app, ["telegram", "test"])
    assert test_res.exit_code == 0
    assert "OK: sent test message to chat 987654321" in test_res.output


def test_cli_telegram_setup_empty_token() -> None:
    """Empty bot token raises validation error."""
    res = runner.invoke(app, ["telegram", "setup", "--token", "   "])
    assert res.exit_code != 0


def test_cli_telegram_test_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telegram API error during test command raises cleanly."""
    mock_prov = MagicMock()
    mock_prov.get.side_effect = lambda k: "some_secret_val"
    monkeypatch.setattr(
        "algodesk.cli.telegram._resolve_secrets_provider",
        lambda rt, **kw: mock_prov,
    )

    def fail_post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "Forbidden"})

    monkeypatch.setattr(httpx.Client, "post", lambda self, url, **kwargs: fail_post(url, **kwargs))

    res = runner.invoke(app, ["telegram", "test"])
    assert res.exit_code != 0
