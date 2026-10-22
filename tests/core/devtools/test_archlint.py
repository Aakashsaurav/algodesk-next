"""Tests for devtools architecture AST linter (CB-1..CB-9)."""

from __future__ import annotations

import json
from pathlib import Path

from algodesk.api.checks import Severity
from algodesk.devtools.archlint import (
    format_archlint_json,
    format_archlint_text,
    make_archlint_check,
    run_archlint,
)


def _setup_minimal_project(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk"
    src_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "archlint.toml").write_text(
        "[banned_calls]\n"
        "banned_functions = ['eval', 'exec', 'os.system']\n"
        "ban_shell_true = true\n\n"
        "[stub_detector]\n"
        "allowed_docstring_exceptions = true\n"
        "return_true_prefixes = ['check', 'is_', 'is', 'verify', 'ping']\n\n"
        "[hard_coded_limits]\n"
        "patterns = ['limit', 'max_', 'min_', 'timeout']\n"
        "ignore_values = [0, 1, -1]\n"
    )
    (cfg_dir / "archlint_allow.toml").write_text("# empty\n")


def test_banned_calls_eval_exec_os_system(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "bad.py").write_text(
        "def run_code():\n"
        "    eval('1 + 1')\n"
        "    exec('x = 2')\n"
        "    import os\n"
        "    os.system('ls')\n"
    )
    rep = run_archlint(tmp_path)
    rules = [e.rule for e in rep.errors]
    assert rules.count("archlint.banned_call") == 3
    assert rep.exit_code == 1


def test_banned_calls_shell_true(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "subproc.py").write_text(
        "import subprocess\ndef launch():\n    subprocess.run(['ls'], shell=True)\n"
    )
    rep = run_archlint(tmp_path)
    assert any(e.rule == "archlint.banned_call" and "shell=True" in e.message for e in rep.errors)


def test_stub_detector_catches_pass_and_ellipsis(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "stubs.py").write_text("def stub_pass():\n    pass\n\ndef stub_dots():\n    ...\n")
    rep = run_archlint(tmp_path)
    stub_errs = [e for e in rep.errors if e.rule == "archlint.stub_detector"]
    assert len(stub_errs) == 2


def test_stub_detector_catches_trivial_return_true(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "checks.py").write_text(
        "def verify_broker():\n    '''Verify broker connection.'''\n    return True\n"
    )
    rep = run_archlint(tmp_path)
    assert any(
        e.rule == "archlint.stub_detector" and "verify_broker" in e.message for e in rep.errors
    )


def test_stub_detector_allows_exception_classes_with_docstrings(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "errs.py").write_text(
        "class MyCustomError(Exception):\n"
        "    '''Docstring only.'''\n\n"
        "class OtherError(Exception):\n"
        "    pass\n"
    )
    rep = run_archlint(tmp_path)
    assert len(rep.errors) == 0


def test_stub_detector_allows_protocols_and_abstractmethods(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "proto.py").write_text(
        "from typing import Protocol\n"
        "from abc import abstractmethod\n\n"
        "class MyProtocol(Protocol):\n"
        "    def do_action(self) -> None:\n"
        "        ...\n\n"
        "class MyBase:\n"
        "    @abstractmethod\n"
        "    def execute(self) -> None:\n"
        "        pass\n"
    )
    rep = run_archlint(tmp_path)
    assert len(rep.errors) == 0


def test_hard_coded_limits_variable_and_param_and_call(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "limits.py").write_text(
        "max_retry_limit = 5\n"
        "def query(timeout_ms: int = 5000):\n"
        "    pass\n"
        "def caller():\n"
        "    client_call(timeout=30)\n"
    )
    rep = run_archlint(tmp_path)
    limit_errs = [e for e in rep.errors if e.rule == "archlint.hard_coded_limit"]
    assert len(limit_errs) == 3


def test_hard_coded_limits_allows_ignore_values(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "safe.py").write_text("min_val = 0\nmax_val = 1\nlimit = -1\n")
    rep = run_archlint(tmp_path)
    assert len(rep.errors) == 0


def test_plugin_imports_valid_and_unauthorized(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    p_dir = tmp_path / "plugins" / "venue" / "myvenue"
    p_dir.mkdir(parents=True)
    (p_dir / "plugin.toml").write_text(
        "[plugin]\nname='myvenue'\nkind='venue'\nversion='0.1'\n[requires]\npackages=['requests']\n"
    )
    (p_dir / "helper.py").write_text("def assist(): pass\n")
    (p_dir / "entry.py").write_text(
        "import sys\n"
        "import requests\n"
        "from algodesk.api import Plugin\n"
        "from .helper import assist\n"
        "import algodesk.journal\n"  # UNAUTHORIZED core package
    )
    rep = run_archlint(tmp_path)
    assert any(
        e.rule == "archlint.plugin_import" and "algodesk.journal" in e.message for e in rep.errors
    )


def test_duplicate_class_names_detected(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "file1.py").write_text("class ConflictingName:\n    x = 1\n")
    (src_dir / "file2.py").write_text("class ConflictingName:\n    y = 2\n")

    rep = run_archlint(tmp_path)
    assert any(
        e.rule == "archlint.duplicate_class" and "ConflictingName" in e.message for e in rep.errors
    )


def test_allow_list_suppresses_finding(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "limits.py").write_text("max_retry_limit = 5\n")

    # Put matching allow entry in config/archlint_allow.toml
    cfg_allow = tmp_path / "config" / "archlint_allow.toml"
    cfg_allow.write_text(
        "[[allow]]\n"
        "file = 'src/algodesk/limits.py'\n"
        "rule = 'archlint.hard_coded_limit'\n"
        "symbol = 'max_retry_limit'\n"
        "reason = 'Display pagination limit'\n"
    )
    rep = run_archlint(tmp_path)
    assert len(rep.errors) == 0


def test_allow_list_empty_reason_is_error(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    cfg_allow = tmp_path / "config" / "archlint_allow.toml"
    cfg_allow.write_text(
        "[[allow]]\n"
        "file = 'limits.py'\n"
        "rule = 'archlint.hard_coded_limit'\n"
        "symbol = 'max_limit'\n"
        "reason = ''\n"  # EMPTY!
    )
    rep = run_archlint(tmp_path)
    assert any(e.rule == "archlint.empty_reason" for e in rep.errors)


def test_allow_list_stale_entry_is_error(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "clean.py").write_text("x = 1\n")

    cfg_allow = tmp_path / "config" / "archlint_allow.toml"
    cfg_allow.write_text(
        "[[allow]]\n"
        "file = 'clean.py'\n"
        "rule = 'archlint.hard_coded_limit'\n"
        "symbol = 'ghost_limit'\n"
        "reason = 'Historical reason'\n"
    )
    rep = run_archlint(tmp_path)
    assert any(e.rule == "archlint.stale_allow_entry" for e in rep.errors)


def test_format_archlint_text_and_json(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "clean.py").write_text("val = 1\n")
    rep = run_archlint(tmp_path)
    txt = format_archlint_text(rep)
    assert txt.startswith("LINT: 1 files checked, 0 errors, 0 warnings")

    raw_json = format_archlint_json(rep)
    data = json.loads(raw_json)
    assert data["summary"]["checked"] == 1
    assert data["summary"]["errors"] == 0


def test_make_archlint_check_doctor(tmp_path: Path) -> None:
    class FakeRuntime:
        root = tmp_path

    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "clean.py").write_text("val = 1\n")

    rt = FakeRuntime()
    chk = make_archlint_check(rt)
    assert chk.id == "lint.architecture"
    assert chk.severity == Severity.CRITICAL

    res_ok = chk.run()
    assert res_ok.ok is True

    (src_dir / "bad.py").write_text("eval('1+1')\n")
    res_fail = chk.run()
    assert res_fail.ok is False
    assert "Architecture lint failed" in res_fail.message


def test_archlint_detects_exec_and_eval_in_nested_functions(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    code = "def outer():\n    def inner():\n        eval('x + 1')\n    return inner\n"
    (src_dir / "nested.py").write_text(code)
    rep = run_archlint(tmp_path)
    errs = [e for e in rep.errors if e.rule == "archlint.banned_call"]
    assert len(errs) == 1
    assert "eval" in errs[0].message


def test_archlint_allows_legitimate_not_supported_exception(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    code = (
        "from algodesk.api.errors import NotSupported\n\n"
        "def run_order():\n"
        "    raise NotSupported('Not supported feature')\n"
    )
    (src_dir / "order.py").write_text(code)
    rep = run_archlint(tmp_path)
    # Raising NotSupported should NOT be flagged as a forbidden stub
    assert not any(e.rule == "archlint.stub_detected" for e in rep.errors)


def test_archlint_flags_ellipsis_stub(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    code = """def run_trade():
    ...
"""
    (src_dir / "trade.py").write_text(code)
    rep = run_archlint(tmp_path)
    errs = [e for e in rep.errors if e.rule == "archlint.stub_detector"]
    assert len(errs) == 1
    assert "..." in errs[0].message


def test_archlint_banned_subprocess_shell_true_keyword(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    code = "import subprocess\ndef call_cmd():\n    subprocess.run('ls', shell=True)\n"
    (src_dir / "cmd.py").write_text(code)
    rep = run_archlint(tmp_path)
    errs = [e for e in rep.errors if e.rule == "archlint.banned_call"]
    assert len(errs) == 1
    assert "shell=True" in errs[0].message


def test_archlint_detects_duplicate_class_name_across_modules(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "mod_a.py").write_text("class OrderManager:\n    pass\n")
    (src_dir / "mod_b.py").write_text("class OrderManager:\n    pass\n")
    rep = run_archlint(tmp_path)
    errs = [e for e in rep.errors if e.rule == "archlint.duplicate_class"]
    assert len(errs) == 1
    assert "OrderManager" in errs[0].message


def test_archlint_handles_syntax_error_file_gracefully(tmp_path: Path) -> None:
    _setup_minimal_project(tmp_path)
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "broken_syntax.py").write_text("def broken(\n")
    rep = run_archlint(tmp_path)
    errs = [e for e in rep.errors if e.rule == "archlint.syntax_error"]
    assert len(errs) == 1
