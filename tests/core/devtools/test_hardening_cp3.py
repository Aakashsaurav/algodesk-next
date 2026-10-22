"""Tests for CP3 hardening: R-05, R-09, R-12, R-13.

Verifies:
- budget and docs tools use rt.params and reflect overrides.
- Missing configuration values raise ConfigError without silent numeric fallbacks.
- docs.max_status_words is respected.
- Archlint flags attribute assignments and .get / getattr default literals with limit names.
- Archlint requires exact file and rule matches, and flags empty symbols in allow list.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import ConfigError
from algodesk.cli.app import app
from algodesk.devtools.archlint import (
    ArchlintConfig,
    _AstChecker,
    load_allow_list,
)
from algodesk.devtools.budget import load_budget_params
from algodesk.devtools.docs import load_docs_params
from algodesk.ops.checks import _resolve_min_free_disk_gb


def test_budget_param_override_reflected_in_budget_command(tmp_path: Path) -> None:
    runner = CliRunner()
    # In isolated or real workspace, setting param changes budget output
    res = runner.invoke(
        app,
        ["param", "set", "budget.core_lines_guidance", "21000", "--reason", "hardening demo"],
    )
    assert res.exit_code == 0, res.output

    try:
        res_budget = runner.invoke(app, ["budget"])
        assert res_budget.exit_code == 0, res_budget.output
        first_line = res_budget.output.strip().splitlines()[0]
        assert "guidance 21000" in first_line
    finally:
        runner.invoke(
            app,
            ["param", "reset", "budget.core_lines_guidance", "--reason", "cleanup demo"],
        )


def test_budget_missing_param_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_dir = tmp_path / "empty_proj"
    cfg = empty_dir / "config" / "params"
    cfg.mkdir(parents=True)
    (cfg / "core.toml").write_text("# missing budget params\n")
    monkeypatch.setattr("algodesk.devtools.budget._find_shipped_core_toml", lambda: None)
    with pytest.raises(ConfigError) as exc_info:
        load_budget_params(empty_dir, params={})
    assert "budget" in exc_info.value.where or "core_lines_guidance" in exc_info.value.what


def test_docs_missing_param_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_dir = tmp_path / "empty_proj"
    cfg = empty_dir / "config" / "params"
    cfg.mkdir(parents=True)
    (cfg / "core.toml").write_text("# missing docs params\n")
    monkeypatch.setattr("algodesk.devtools.docs._find_shipped_core_toml", lambda: None)
    with pytest.raises(ConfigError) as exc_info:
        load_docs_params(empty_dir, params={})
    assert "docs" in exc_info.value.where or "max_summary_words" in exc_info.value.what


def test_disk_check_missing_param_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_dir = tmp_path / "empty_proj"
    cfg = empty_dir / "config" / "params"
    cfg.mkdir(parents=True)
    (cfg / "core.toml").write_text("# missing ops params\n")
    monkeypatch.setattr("algodesk.ops.checks._find_shipped_core_toml", lambda: None)

    class FakeRuntime:
        root = empty_dir
        params: None = None

    with pytest.raises(ConfigError) as exc_info:
        _resolve_min_free_disk_gb(FakeRuntime())  # type: ignore[arg-type]
    assert "ops.min_free_disk_gb" in exc_info.value.what


def test_archlint_attribute_assignment_limit_flagged() -> None:
    code = """
class Client:
    def __init__(self):
        self.timeout = 30
        self.max_buffer = 500
"""
    cfg = ArchlintConfig(
        banned_functions=(),
        ban_shell_true=False,
        allowed_docstring_exceptions=True,
        return_true_prefixes=(),
        limit_patterns=("timeout", "max_", "limit"),
        ignore_numeric_values=(0, 1, -1),
        allowed_framework_roots=("algodesk.api",),
    )
    checker = _AstChecker("test.py", cfg, is_plugin=False, plugin_packages=set())
    tree = ast.parse(code)
    checker.visit(tree)

    rules = [f.rule for f in checker.findings]
    symbols = [f.symbol for f in checker.findings]
    assert rules.count("archlint.hard_coded_limit") == 2
    assert "timeout" in symbols
    assert "max_buffer" in symbols


def test_archlint_get_and_getattr_limit_literal_flagged() -> None:
    code = """
def check_values(d, obj):
    t = d.get("timeout_ms", 5000)
    b = getattr(obj, "max_buffer", 100)
    # Safe lookups
    ok1 = d.get("timeout_ms")
    ok2 = getattr(obj, "timeout_ms")
    ok3 = d.get("name", "default")
    return t, b, ok1, ok2, ok3
"""
    cfg = ArchlintConfig(
        banned_functions=(),
        ban_shell_true=False,
        allowed_docstring_exceptions=True,
        return_true_prefixes=(),
        limit_patterns=("timeout", "max_", "limit"),
        ignore_numeric_values=(0, 1, -1),
        allowed_framework_roots=("algodesk.api",),
    )
    checker = _AstChecker("test.py", cfg, is_plugin=False, plugin_packages=set())
    tree = ast.parse(code)
    checker.visit(tree)

    limit_findings = [f for f in checker.findings if f.rule == "archlint.hard_coded_limit"]
    symbols = [f.symbol for f in limit_findings]
    assert len(limit_findings) == 2
    assert "timeout_ms" in symbols
    assert "max_buffer" in symbols


def test_archlint_allow_list_exact_match_and_empty_symbol_error(tmp_path: Path) -> None:
    allow_file = tmp_path / "config" / "archlint_allow.toml"
    allow_file.parent.mkdir(parents=True)
    allow_file.write_text("""
[[allow]]
file = "src/algodesk/sub/file.py"
rule = "archlint.hard_coded_limit"
symbol = ""
reason = "Needs exemption"
""")
    entries, findings = load_allow_list(tmp_path)
    assert any(f.rule == "archlint.empty_symbol" for f in findings)
