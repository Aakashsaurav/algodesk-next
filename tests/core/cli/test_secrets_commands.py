"""Tests for CLI secrets commands (set, list, delete)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from algodesk.cli.app import app


def test_cli_secrets_env_provider(runner: CliRunner, monkeypatch: object) -> None:
    # Test list and set with env provider
    res_list = runner.invoke(app, ["secrets", "list", "--provider", "env"])
    assert res_list.exit_code == 0

    # Setting on env provider raises NotSupported -> exit code 1
    res_set = runner.invoke(
        app,
        ["secrets", "set", "my_token", "--stdin", "--provider", "env"],
        input="some-val\n",
    )
    assert res_set.exit_code == 1
    assert "ERROR:" in res_set.output


def test_cli_secrets_roundtrip_keychain(runner: CliRunner, tmp_project: Path) -> None:
    # On macOS, keychain is available
    res_set = runner.invoke(
        app,
        ["--root", str(tmp_project), "secrets", "set", "test_cli_key", "--stdin"],
        input="cli-test-secret-value\n",
    )
    assert res_set.exit_code == 0
    assert "stored successfully" in res_set.output

    res_list = runner.invoke(app, ["--root", str(tmp_project), "secrets", "list"])
    assert res_list.exit_code == 0
    assert "test_cli_key" in res_list.output
    # Secret value is NEVER in output
    assert "cli-test-secret-value" not in res_list.output

    res_del = runner.invoke(app, ["--root", str(tmp_project), "secrets", "delete", "test_cli_key"])
    assert res_del.exit_code == 0
    assert "deleted" in res_del.output

    res_list_after = runner.invoke(app, ["--root", str(tmp_project), "secrets", "list"])
    assert "test_cli_key" not in res_list_after.output
