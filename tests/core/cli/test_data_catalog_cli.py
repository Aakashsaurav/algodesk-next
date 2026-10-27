"""Tests for data catalog, delete, trash, synth, and groups CLI commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from algodesk.cli.app import app
from algodesk.ops.paths import find_project_root


def test_cli_groups_list_and_show(runner: CliRunner) -> None:
    root = find_project_root()
    res = runner.invoke(app, ["--root", str(root), "groups", "list"])
    assert res.exit_code == 0
    assert "demo5" in res.output

    res_json = runner.invoke(app, ["--root", str(root), "groups", "list", "--json"])
    assert res_json.exit_code == 0
    assert "demo5" in res_json.output

    res_show = runner.invoke(app, ["--root", str(root), "groups", "show", "demo5"])
    assert res_show.exit_code == 0
    assert "NSE:EQ:HDFCBANK" in res_show.output


def test_cli_data_synth_and_list(runner: CliRunner, tmp_path: Path) -> None:
    root = find_project_root()
    # Synth ticks
    res = runner.invoke(
        app,
        [
            "--root",
            str(root),
            "data",
            "synth",
            "--symbols",
            "NSE:EQ:HDFCBANK,NSE:EQ:SBIN",
            "--days",
            "2",
            "--rows",
            "50",
            "--seed",
            "10",
        ],
    )
    assert res.exit_code == 0
    assert "Generated 200 synthetic ticks" in res.output

    # List data coverage
    res_list = runner.invoke(app, ["--root", str(root), "data", "list", "--group-by", "exchange"])
    assert res_list.exit_code == 0
    assert "NSE" in res_list.output
    assert "synthetic" in res_list.output


def test_cli_data_delete_dry_run_and_confirm(runner: CliRunner) -> None:
    root = find_project_root()
    # Ensure there is data to delete
    runner.invoke(
        app,
        [
            "--root",
            str(root),
            "data",
            "synth",
            "--symbols",
            "NSE:EQ:HDFCBANK",
            "--days",
            "1",
            "--rows",
            "10",
            "--seed",
            "99",
        ],
    )
    # Dry run
    res_dry = runner.invoke(
        app,
        [
            "--root",
            str(root),
            "data",
            "delete",
            "HDFCBANK",
            "--dry-run",
            "--reason",
            "test dry run",
        ],
    )
    assert res_dry.exit_code == 0
    assert "Plan:" in res_dry.output

    # Delete with --yes
    res_del = runner.invoke(
        app,
        ["--root", str(root), "data", "delete", "HDFCBANK", "--yes", "--reason", "test delete yes"],
    )
    assert res_del.exit_code == 0, f"Delete failed with output:\n{res_del.output}"
    assert "Successfully deleted" in res_del.output

    # Trash list
    res_trash = runner.invoke(app, ["--root", str(root), "data", "trash", "list"])
    assert res_trash.exit_code == 0

    # Trash purge with --yes
    res_purge = runner.invoke(app, ["--root", str(root), "data", "trash", "purge", "--yes"])
    assert res_purge.exit_code == 0
    assert "Purged" in res_purge.output
