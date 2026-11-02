"""Tests for devtools CLI commands and doctor integration."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from algodesk.cli.app import app
from algodesk.ops.paths import find_project_root


def test_cli_budget_command_text(runner: CliRunner) -> None:
    root = find_project_root()
    res = runner.invoke(app, ["--root", str(root), "budget"])
    assert res.exit_code == 0
    assert res.output.startswith("BUDGET: core")
    assert "Package / Plugin" in res.output
    assert "Total core" in res.output


def test_cli_budget_command_json(runner: CliRunner) -> None:
    root = find_project_root()
    res = runner.invoke(app, ["--root", str(root), "budget", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert "summary" in data
    assert "packages" in data
    assert "dependencies" in data


def test_cli_budget_fail_exits_code_one(runner: CliRunner, tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk" / "mod"
    src_dir.mkdir(parents=True)
    (src_dir / "app.py").write_text("x = 1\n")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'algodesk'\ndependencies = ['d1', 'd2', 'd3']\n"
    )
    cfg = tmp_path / "config" / "params"
    cfg.mkdir(parents=True)
    (cfg / "core.toml").write_text(
        "[[param]]\nname='budget.max_core_dependencies'\ndefault=1\n"
        "[[param]]\nname='budget.max_runtime_dependencies'\ndefault=2\n"
        "[[param]]\nname='budget.core_lines_guidance'\ndefault=100\n"
        "[[param]]\nname='budget.core_modules_guidance'\ndefault=10\n"
        "[[param]]\nname='budget.plugin_lines_guidance'\ndefault=50\n"
        "[[param]]\nname='budget.warn_overshoot_pct'\ndefault=25.0\n"
    )
    res = runner.invoke(app, ["--root", str(tmp_path), "budget"])
    assert res.exit_code == 1
    assert "FAIL" in res.output


def test_cli_docs_check_success(runner: CliRunner) -> None:
    root = find_project_root()
    res = runner.invoke(app, ["--root", str(root), "docs", "check"])
    assert res.exit_code == 0
    assert "DOCS:" in res.output
    assert "0 errors" in res.output


def test_cli_docs_check_json(runner: CliRunner) -> None:
    root = find_project_root()
    res = runner.invoke(app, ["--root", str(root), "docs", "check", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert "summary" in data
    assert data["summary"]["errors"] == 0


def test_cli_docs_check_fail_code_one(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'algodesk'\n")
    (tmp_path / "README.md").write_text("Missing summary heading\n")
    res = runner.invoke(app, ["--root", str(tmp_path), "docs", "check"])
    assert res.exit_code == 1
    assert "docs.summary_heading" in res.output


def test_cli_docs_index_regenerates(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'algodesk'\n")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "STATUS.md").write_text("## Summary\n\nStatus doc.\n")
    res = runner.invoke(app, ["--root", str(tmp_path), "docs", "index"])
    assert res.exit_code == 0
    assert "OK: regenerated docs/INDEX.md" in res.output
    assert (docs_dir / "INDEX.md").is_file()


def test_cli_lint_command_text(runner: CliRunner) -> None:
    root = find_project_root()
    res = runner.invoke(app, ["--root", str(root), "lint"])
    assert res.exit_code == 0
    assert "LINT:" in res.output
    assert "0 errors" in res.output


def test_cli_lint_command_json(runner: CliRunner) -> None:
    root = find_project_root()
    res = runner.invoke(app, ["--root", str(root), "lint", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert "summary" in data
    assert data["summary"]["errors"] == 0


def test_cli_lint_fail_code_one(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'algodesk'\n")
    src = tmp_path / "src" / "algodesk"
    src.mkdir(parents=True)
    (src / "bad.py").write_text("eval('1+1')\n")
    res = runner.invoke(app, ["--root", str(tmp_path), "lint"])
    assert res.exit_code == 1
    assert "archlint.banned_call" in res.output


def test_cli_secrets_scan_success(runner: CliRunner) -> None:
    root = find_project_root()
    res = runner.invoke(app, ["--root", str(root), "secrets", "scan"])
    assert res.exit_code == 0
    assert "SECRETS:" in res.output
    assert "0 findings" in res.output


def test_cli_secrets_scan_json(runner: CliRunner) -> None:
    root = find_project_root()
    res = runner.invoke(app, ["--root", str(root), "secrets", "scan", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert "summary" in data
    assert data["summary"]["findings"] == 0


def test_cli_secrets_scan_fail_code_one(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'algodesk'\n")
    raw_secret = "API_KEY = 'super_high_entropy_token_9999'\n"  # fake-secret
    (tmp_path / "bad.env").write_text(raw_secret)
    res = runner.invoke(app, ["--root", str(tmp_path), "secrets", "scan"])
    assert res.exit_code == 1
    assert "secrets.quoted_secret" in res.output


def test_cli_doctor_runs_fifteen_checks(runner: CliRunner) -> None:
    root = find_project_root()
    res = runner.invoke(app, ["--root", str(root), "doctor"])
    assert res.exit_code == 0
    assert "DOCTOR: 19 passed, 0 warning, 0 critical" in res.output


def test_cli_doctor_check_ids_include_devtools(runner: CliRunner) -> None:
    root = find_project_root()
    res = runner.invoke(app, ["--root", str(root), "doctor", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    check_ids = [r["id"] for r in data["results"]]
    assert len(check_ids) == 19
    assert "disk.ticks_projection" in check_ids
    assert "budget.dependencies" in check_ids
    assert "budget.size" in check_ids
    assert "docs.rules" in check_ids
    assert "lint.architecture" in check_ids
    assert "secrets.scan" in check_ids
    assert "data.verify_quick" in check_ids
    assert "data.quarantine" in check_ids
    assert "data.warmup_source" in check_ids


def test_cli_budget_help(runner: CliRunner) -> None:
    res = runner.invoke(app, ["budget", "--help"])
    assert res.exit_code == 0
    assert "Evaluate codebase size" in res.output


def test_cli_docs_check_help(runner: CliRunner) -> None:
    res = runner.invoke(app, ["docs", "check", "--help"])
    assert res.exit_code == 0
    assert "Validate documentation" in res.output


def test_cli_docs_index_help(runner: CliRunner) -> None:
    res = runner.invoke(app, ["docs", "index", "--help"])
    assert res.exit_code == 0
    assert "Regenerate deterministic" in res.output


def test_cli_lint_help(runner: CliRunner) -> None:
    res = runner.invoke(app, ["lint", "--help"])
    assert res.exit_code == 0
    assert "Run AST architecture rules" in res.output


def test_cli_secrets_scan_help(runner: CliRunner) -> None:
    res = runner.invoke(app, ["secrets", "scan", "--help"])
    assert res.exit_code == 0
    assert "Scan codebase for exposed secrets" in res.output


def test_check_sh_has_required_steps() -> None:
    """CP0 step 2: Light test that parses scripts/check.sh for the required steps."""
    root = find_project_root()
    script = root / "scripts" / "check.sh"
    assert script.exists()
    content = script.read_text(encoding="utf-8")

    required_steps = [
        "isolation",
        "ruff check",
        "ruff format",
        "mypy",
        "pytest",
        "lint-imports",
        "budget",
        "docs check",
        "lint",
        "secrets scan",
    ]
    for step in required_steps:
        assert step in content, f"Step '{step}' missing from scripts/check.sh"
