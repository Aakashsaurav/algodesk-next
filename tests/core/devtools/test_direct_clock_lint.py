"""Tests for the direct_clock architecture lint rule."""

from __future__ import annotations

import ast

from algodesk.devtools.archlint import ArchlintConfig, _AstChecker


def _run_checker_on_code(code: str, rel_path: str) -> list[str]:
    cfg = ArchlintConfig(
        banned_functions=("eval", "exec"),
        ban_shell_true=True,
        allowed_docstring_exceptions=True,
        return_true_prefixes=("check",),
        limit_patterns=("limit",),
        ignore_numeric_values=(0, 1),
        allowed_framework_roots=("algodesk.api",),
        direct_clock_symbols=("datetime.now", "datetime.utcnow", "time.time"),
        direct_clock_prefixes=(
            "src/algodesk/engine",
            "src/algodesk/oms",
            "src/algodesk/risk",
            "src/algodesk/portfolio",
            "src/algodesk/results",
            "src/algodesk/strategies",
            "plugins/strategies",
        ),
        direct_clock_exempt=("src/algodesk/engine/clock.py",),
    )
    checker = _AstChecker(rel_path, cfg, is_plugin=False, plugin_packages=set())
    tree = ast.parse(code)
    checker.visit(tree)
    return [f.rule for f in checker.findings]


def test_direct_clock_violation_in_engine() -> None:
    code = """
import datetime

def bad():
    return datetime.datetime.now()
"""
    rules = _run_checker_on_code(code, "src/algodesk/engine/bars.py")
    assert "archlint.direct_clock" in rules


def test_direct_clock_utcnow_violation() -> None:
    code = """
from datetime import datetime

def bad():
    return datetime.utcnow()
"""
    rules = _run_checker_on_code(code, "src/algodesk/risk/engine.py")
    assert "archlint.direct_clock" in rules


def test_direct_clock_time_time_violation() -> None:
    code = """
import time

def bad():
    return time.time()
"""
    rules = _run_checker_on_code(code, "src/algodesk/oms/manager.py")
    assert "archlint.direct_clock" in rules


def test_direct_clock_exempt_in_clock_py() -> None:
    code = """
import datetime
import time

def allowed():
    n = datetime.datetime.now()
    t = time.time()
    return n, t
"""
    rules = _run_checker_on_code(code, "src/algodesk/engine/clock.py")
    assert "archlint.direct_clock" not in rules


def test_direct_clock_not_enforced_in_cli_or_journal() -> None:
    code = """
from datetime import datetime, timezone

def ok():
    return datetime.now(timezone.utc)
"""
    rules = _run_checker_on_code(code, "src/algodesk/cli/app.py")
    assert "archlint.direct_clock" not in rules
