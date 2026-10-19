"""Tests for algodesk config CLI commands."""

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from algodesk.cli.app import app


def test_config_show_human(runner: CliRunner, make_project: Any) -> None:
    """config show prints rendered effective configuration."""
    proj = make_project("cfg_human")
    result = runner.invoke(app, ["--root", str(proj), "config", "show"])
    assert result.exit_code == 0
    assert "EFFECTIVE CONFIG" in result.output
    assert "project = algodesk" in result.output


def test_config_show_json(runner: CliRunner, make_project: Any) -> None:
    """config show --json outputs valid JSON representation."""
    proj = make_project("cfg_json")
    result = runner.invoke(app, ["--json", "--root", str(proj), "config", "show"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["project"] == "algodesk"
    assert "paths" in data
