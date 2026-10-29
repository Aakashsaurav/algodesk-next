"""Tests for AST-based API index generation and CLI command."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from algodesk.cli.app import app
from algodesk.devtools.api_index import generate_api_index
from algodesk.ops.paths import find_project_root


def test_api_index_generation() -> None:
    root = find_project_root()
    content = generate_api_index(root)

    assert content.startswith("# AlgoDesk Next API Index")
    assert "## Summary" in content

    # Known public names from packages under src/algodesk
    known_names = [
        "AlgoDeskError",
        "app",
        "DataManifest",
        "write_api_index",
        "BarBuilder",
        "FeeEngine",
        "Journal",
        "setup_logging",
        "open_runtime",
        "ParamRegistry",
        "PluginRegistry",
    ]
    for name in known_names:
        assert name in content, f"Known symbol '{name}' missing from API index"


def test_api_index_cli_command(tmp_project: Path) -> None:
    runner = CliRunner()
    root = tmp_project
    res = runner.invoke(app, ["--root", str(root), "api", "index"])
    assert res.exit_code == 0
    assert "OK: generated" in res.output
    out_file = root / "_workbench" / "API_INDEX.md"
    assert out_file.is_file()
