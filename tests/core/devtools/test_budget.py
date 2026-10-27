"""Tests for devtools budget tool (CB-1..CB-9)."""

from __future__ import annotations

import json
from pathlib import Path

from algodesk.api.checks import Severity
from algodesk.devtools.budget import (
    count_file_lines,
    evaluate_metric_status,
    extract_pyproject_dependencies,
    format_budget_json,
    format_budget_text,
    load_budget_params,
    make_budget_checks,
    run_budget,
    scan_core_dependencies,
    scan_core_packages,
    scan_plugins,
)


def test_count_file_lines_ignores_blank_and_comments(tmp_path: Path) -> None:
    f = tmp_path / "sample.py"
    f.write_text("# Comment\n\nx = 1\n   # indented comment\n   \ny = 2\n")
    assert count_file_lines(f) == 2


def test_count_file_lines_nonexistent_returns_zero(tmp_path: Path) -> None:
    assert count_file_lines(tmp_path / "absent.py") == 0


def test_scan_core_dependencies(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk"
    pkg = src_dir / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text(
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "import pydantic\n"
        "from typer import Typer\n"
        "from . import sub\n"
        "import algodesk.api\n"
    )
    deps = scan_core_dependencies(src_dir)
    assert deps == ["pydantic", "typer"]


def test_scan_core_packages_skips_empty_init(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk"
    pkg_dir = src_dir / "mypkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("# Only comments\n\n")
    (pkg_dir / "mod.py").write_text("a = 10\n")

    packages, total_lines, total_mods = scan_core_packages(src_dir, 25.0)
    assert len(packages) == 1
    assert packages[0].name == "mypkg"
    assert packages[0].lines == 1
    assert packages[0].modules == 1
    assert total_lines == 1
    assert total_mods == 1


def test_scan_core_packages_excludes_devtools_and_pycache(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk"
    (src_dir / "devtools").mkdir(parents=True)
    (src_dir / "devtools" / "tool.py").write_text("x = 1\n")
    (src_dir / "mypkg" / "__pycache__").mkdir(parents=True)
    (src_dir / "mypkg" / "__pycache__" / "cached.py").write_text("x = 1\n")
    (src_dir / "mypkg" / "real.py").write_text("val = 42\n")

    packages, total_lines, total_mods = scan_core_packages(src_dir, 25.0)
    names = [p.name for p in packages]
    assert "devtools" not in names
    assert "__pycache__" not in names
    assert "mypkg" in names
    assert total_lines == 1
    assert total_mods == 1


def test_scan_core_packages_includes_root_modules(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("export = True\n")
    (src_dir / "__main__.py").write_text("run = True\n")

    packages, total_lines, total_mods = scan_core_packages(src_dir, 25.0)
    root_pkg = next((p for p in packages if p.name == "(root)"), None)
    assert root_pkg is not None
    assert root_pkg.modules == 2
    assert root_pkg.lines == 2
    assert total_lines == 2
    assert total_mods == 2


def test_scan_plugins_reads_manifest_and_code(tmp_path: Path) -> None:
    plugins_dir = tmp_path / "plugins"
    p1 = plugins_dir / "notifier" / "telegram"
    p1.mkdir(parents=True)
    (p1 / "plugin.toml").write_text(
        "[plugin]\n"
        "name = 'telegram'\n"
        "kind = 'notifier'\n"
        "version = '0.1.0'\n\n"
        "[requires]\n"
        "packages = ['requests>=2.28', 'aiohttp']\n"
    )
    (p1 / "client.py").write_text("def send(): pass\n")

    budgets, req_pkgs = scan_plugins(plugins_dir, 1500, 25.0)
    assert len(budgets) == 1
    assert budgets[0].name == "notifier/telegram"
    assert budgets[0].lines == 1
    assert budgets[0].modules == 1
    assert req_pkgs == ["aiohttp", "requests"]


def test_extract_pyproject_dependencies(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\nname = 'test'\ndependencies = ['pydantic>=2.0', 'typer>=0.9.0', 'foo-bar<3']\n"
    )
    deps = extract_pyproject_dependencies(pyproject)
    assert deps == ["foo-bar", "pydantic", "typer"]


def test_evaluate_metric_status() -> None:
    assert evaluate_metric_status(100, 100, 25.0) == "ok"
    assert evaluate_metric_status(90, 100, 25.0) == "ok"
    assert evaluate_metric_status(110, 100, 25.0) == "advisory"
    assert evaluate_metric_status(125, 100, 25.0) == "advisory"
    assert evaluate_metric_status(126, 100, 25.0) == "review"


def test_run_budget_ok_status(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk" / "pkg"
    src_dir.mkdir(parents=True)
    (src_dir / "mod.py").write_text("import pydantic\nx = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = ['pydantic']\n")

    params = {
        "budget.core_lines_guidance": 100,
        "budget.core_modules_guidance": 10,
        "budget.plugin_lines_guidance": 50,
        "budget.warn_overshoot_pct": 25.0,
        "budget.max_runtime_dependencies": 5,
        "budget.max_core_dependencies": 2,
    }
    rep = run_budget(tmp_path, params)
    assert rep.summary.status == "OK"
    assert rep.exit_code == 0
    assert rep.summary.core_lines == 2
    assert rep.summary.core_modules == 1
    assert rep.summary.core_deps == 1


def test_run_budget_advisory_when_lines_exceed_guidance(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk" / "pkg"
    src_dir.mkdir(parents=True)
    code = "\n".join(f"x_{i} = {i}" for i in range(110))
    (src_dir / "mod.py").write_text(code + "\n")
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = []\n")

    params = {
        "budget.core_lines_guidance": 100,
        "budget.core_modules_guidance": 10,
        "budget.plugin_lines_guidance": 50,
        "budget.warn_overshoot_pct": 25.0,
        "budget.max_runtime_dependencies": 5,
        "budget.max_core_dependencies": 2,
    }
    rep = run_budget(tmp_path, params)
    assert rep.summary.status == "ADVISORY"
    assert rep.exit_code == 0  # Advisory does not fail


def test_run_budget_fail_when_core_dependencies_exceed(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk" / "pkg"
    src_dir.mkdir(parents=True)
    (src_dir / "mod.py").write_text("import dep1\nimport dep2\nimport dep3\nx = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = ['dep1', 'dep2', 'dep3']\n")

    params = {
        "budget.core_lines_guidance": 100,
        "budget.core_modules_guidance": 10,
        "budget.plugin_lines_guidance": 50,
        "budget.warn_overshoot_pct": 25.0,
        "budget.max_runtime_dependencies": 5,
        "budget.max_core_dependencies": 2,  # 3 > 2 -> FAIL
    }
    rep = run_budget(tmp_path, params)
    assert rep.summary.status == "FAIL"
    assert rep.exit_code == 1


def test_run_budget_fail_when_total_dependencies_exceed(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk" / "pkg"
    src_dir.mkdir(parents=True)
    (src_dir / "mod.py").write_text("x = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = ['core1']\n")

    plugins_dir = tmp_path / "plugins" / "venue" / "fake"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "plugin.toml").write_text(
        "[plugin]\nname='fake'\nkind='venue'\nversion='0.1'\n"
        "[requires]\npackages=['p1', 'p2', 'p3']\n"
    )

    params = {
        "budget.core_lines_guidance": 100,
        "budget.core_modules_guidance": 10,
        "budget.plugin_lines_guidance": 50,
        "budget.warn_overshoot_pct": 25.0,
        "budget.max_runtime_dependencies": 3,  # 4 total > 3 -> FAIL
        "budget.max_core_dependencies": 5,
    }
    rep = run_budget(tmp_path, params)
    assert rep.summary.status == "FAIL"
    assert rep.exit_code == 1


def test_format_budget_text_first_line_format(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk" / "pkg"
    src_dir.mkdir(parents=True)
    (src_dir / "mod.py").write_text("import pydantic\nx = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = ['pydantic']\n")

    params = {
        "budget.core_lines_guidance": 100,
        "budget.core_modules_guidance": 10,
        "budget.plugin_lines_guidance": 50,
        "budget.warn_overshoot_pct": 25.0,
        "budget.max_runtime_dependencies": 5,
        "budget.max_core_dependencies": 2,
    }
    rep = run_budget(tmp_path, params)
    text = format_budget_text(rep)
    lines = text.splitlines()
    expected_first = (
        "BUDGET: core 2 lines (guidance 100) | 1 modules (guidance 10) | "
        "deps 1/2 core, 1/5 total | OK"
    )
    assert lines[0] == expected_first
    assert "Package / Plugin" in text
    assert "pkg" in text
    assert "Total core" in text


def test_format_budget_json_valid_json(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk" / "pkg"
    src_dir.mkdir(parents=True)
    (src_dir / "mod.py").write_text("x = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = ['pydantic']\n")

    rep = run_budget(tmp_path)
    raw = format_budget_json(rep)
    data = json.loads(raw)
    assert "summary" in data
    assert "packages" in data
    assert "plugins" in data
    assert "dependencies" in data
    assert data["summary"]["core_lines"] == 1


def test_make_budget_checks_doctor_passes_and_fails(tmp_path: Path) -> None:
    class FakeRuntime:
        root = tmp_path

    src_dir = tmp_path / "src" / "algodesk" / "pkg"
    src_dir.mkdir(parents=True)
    (src_dir / "mod.py").write_text("x = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = ['pydantic']\n")

    # Create config/params/core.toml for parameter loading
    cfg_dir = tmp_path / "config" / "params"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "core.toml").write_text(
        "[[param]]\nname='budget.max_core_dependencies'\ndefault=2\n"
        "[[param]]\nname='budget.max_runtime_dependencies'\ndefault=5\n"
        "[[param]]\nname='budget.core_lines_guidance'\ndefault=100\n"
        "[[param]]\nname='budget.core_modules_guidance'\ndefault=10\n"
        "[[param]]\nname='budget.plugin_lines_guidance'\ndefault=50\n"
        "[[param]]\nname='budget.warn_overshoot_pct'\ndefault=25.0\n"
    )

    rt = FakeRuntime()
    checks = make_budget_checks(rt)
    assert len(checks) == 2
    deps_check = next(c for c in checks if c.id == "budget.dependencies")
    size_check = next(c for c in checks if c.id == "budget.size")
    assert deps_check.severity == Severity.CRITICAL
    assert size_check.severity == Severity.WARNING

    # Under budget -> both ok
    r_deps = deps_check.run()
    assert r_deps.ok is True
    r_size = size_check.run()
    assert r_size.ok is True

    # Overshoot dependencies -> deps fails
    (src_dir / "mod.py").write_text("import d1\nimport d2\nimport d3\nx = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = ['d1', 'd2', 'd3']\n")
    r_deps_fail = deps_check.run()
    assert r_deps_fail.ok is False
    assert "exceed limit" in r_deps_fail.message


def test_load_budget_params_reads_toml_defaults(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "config" / "params"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "core.toml").write_text(
        "[[param]]\nname='budget.core_lines_guidance'\ndefault=7500\n"
    )
    bp = load_budget_params(tmp_path)
    assert bp["budget.core_lines_guidance"] == 7500


def test_scan_plugins_no_plugins_dir_returns_empty(tmp_path: Path) -> None:
    plugins, reqs = scan_plugins(tmp_path / "absent_plugins", 500, 25.0)
    assert plugins == []
    assert reqs == []


def test_scan_plugins_empty_plugin_dir_returns_entry(tmp_path: Path) -> None:
    p_dir = tmp_path / "plugins" / "my_plugin"
    p_dir.mkdir(parents=True)
    (p_dir / "plugin.toml").write_text("[plugin]\nname = 'my_plugin'\n")
    plugins, reqs = scan_plugins(tmp_path / "plugins", 500, 25.0)
    assert len(plugins) == 1
    assert plugins[0].name == "my_plugin"
    assert plugins[0].lines == 0
    assert plugins[0].modules == 0


def test_evaluate_metric_status_cases() -> None:
    # 0 limit
    assert evaluate_metric_status(10, 0, 25.0) == "review"
    # <= limit
    assert evaluate_metric_status(100, 100, 25.0) == "ok"
    # > limit but <= limit * 1.25 -> advisory
    assert evaluate_metric_status(110, 100, 25.0) == "advisory"
    # > limit * 1.25 -> review
    assert evaluate_metric_status(130, 100, 25.0) == "review"


def test_extract_pyproject_dependencies_missing_pyproject(tmp_path: Path) -> None:
    deps = extract_pyproject_dependencies(tmp_path / "absent_pyproject.toml")
    assert deps == []


def test_format_budget_text_with_overshoot_status(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "algodesk" / "huge"
    src_dir.mkdir(parents=True)
    (src_dir / "mod.py").write_text("\n".join(f"x_{i} = {i}" for i in range(200)) + "\n")
    cfg_dir = tmp_path / "config" / "params"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "core.toml").write_text(
        "[[param]]\nname='budget.core_lines_guidance'\ndefault=50\n"
        "[[param]]\nname='budget.warn_overshoot_pct'\ndefault=25.0\n"
    )
    rep = run_budget(tmp_path)
    text = format_budget_text(rep)
    assert "ADVISORY" in text or "REVIEW" in text


def test_size_check_fails_when_core_lines_overshoot(tmp_path: Path) -> None:
    class FakeRuntime:
        root = tmp_path

    src_dir = tmp_path / "src" / "algodesk" / "pkg"
    src_dir.mkdir(parents=True)
    (src_dir / "mod.py").write_text("\n".join(f"v_{i} = {i}" for i in range(150)) + "\n")
    cfg_dir = tmp_path / "config" / "params"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "core.toml").write_text(
        "[[param]]\nname='budget.core_lines_guidance'\ndefault=100\n"
        "[[param]]\nname='budget.core_modules_guidance'\ndefault=10\n"
        "[[param]]\nname='budget.warn_overshoot_pct'\ndefault=25.0\n"
    )
    rt = FakeRuntime()
    checks = make_budget_checks(rt)
    size_check = next(c for c in checks if c.id == "budget.size")
    res = size_check.run()
    assert res.ok is False
    assert "exceeds guidance" in res.message
