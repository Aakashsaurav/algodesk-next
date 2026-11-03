"""Tests for algodesk plugins and new plugin CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from algodesk.cli.app import app


def test_new_plugin_and_refuse_overwrite(runner: CliRunner, make_project: Any) -> None:
    """new plugin scaffolds skeleton and refuses to overwrite existing plugin directory."""
    proj = make_project("p_new")

    res = runner.invoke(app, ["--root", str(proj), "new", "plugin", "notifier", "slack"])
    assert res.exit_code == 0
    assert "Scaffolded new plugin notifier/slack" in res.output
    assert (proj / "plugins" / "notifier" / "slack" / "plugin.toml").is_file()

    # Re-running scaffold for same plugin must fail and exit 1
    res_dup = runner.invoke(app, ["--root", str(proj), "new", "plugin", "notifier", "slack"])
    assert res_dup.exit_code == 1
    assert "ERROR: Plugin directory already exists and is not empty" in res_dup.output


def test_plugins_list_table_and_json(runner: CliRunner, make_project: Any) -> None:
    """plugins list displays plain table and JSON output with conformance."""
    proj = make_project("p_list")
    runner.invoke(app, ["--root", str(proj), "new", "plugin", "notifier", "webhook"])

    # Table output
    res = runner.invoke(app, ["--root", str(proj), "plugins", "list"])
    assert res.exit_code == 0
    assert "notifier" in res.output
    assert "webhook" in res.output
    assert "none" in res.output

    # JSON output
    res_json = runner.invoke(app, ["--json", "--root", str(proj), "plugins", "list"])
    assert res_json.exit_code == 0
    data = json.loads(res_json.output)
    assert len(data) == 1
    assert data[0]["kind"] == "notifier"
    assert data[0]["name"] == "webhook"
    assert data[0]["conformance"] == "none"


def test_plugins_check_clean_and_issues(runner: CliRunner, make_project: Any) -> None:
    """plugins check passes on clean workspace and reports errors on malformed plugin."""
    proj = make_project("p_check")
    runner.invoke(app, ["--root", str(proj), "new", "plugin", "notifier", "good"])

    res_ok = runner.invoke(app, ["--root", str(proj), "plugins", "check"])
    assert res_ok.exit_code == 0
    assert "all plugins passed discovery and manifest checks" in res_ok.output

    # Break manifest in a new plugin
    bad_dir = proj / "plugins" / "notifier" / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "plugin.toml").write_text("invalid = [toml syntax\n")
    (bad_dir / "README.md").write_text("# Bad\n\n## Summary\nBad plugin.\n")

    res_err = runner.invoke(app, ["--root", str(proj), "plugins", "check"])
    assert res_err.exit_code == 1
    assert "issue(s) detected" in res_err.output


def test_plugins_test_success_flow(runner: CliRunner, make_project: Any) -> None:
    """plugins test executes tests, checks contract, and records conformance."""
    proj = make_project("p_test_flow")
    runner.invoke(app, ["--root", str(proj), "new", "plugin", "notifier", "email"])

    res_test = runner.invoke(app, ["--root", str(proj), "plugins", "test", "notifier", "email"])
    assert res_test.exit_code == 0
    assert "passed conformance suite 'base+plugin-tests'" in res_test.output

    # Status in plugins list should now be current
    res_list = runner.invoke(app, ["--json", "--root", str(proj), "plugins", "list"])
    data = json.loads(res_list.output)
    assert data[0]["conformance"] == "current"


def test_plugins_test_failing_pytest(runner: CliRunner, make_project: Any) -> None:
    """plugins test exits 1 when a test in the tests folder fails."""
    proj = make_project("p_failing_test")
    runner.invoke(app, ["--root", str(proj), "new", "plugin", "notifier", "broken_tests"])

    test_file = proj / "plugins" / "notifier" / "broken_tests" / "tests" / "test_broken_tests.py"
    test_file.write_text("def test_fail(): assert False\n")

    res = runner.invoke(app, ["--root", str(proj), "plugins", "test", "notifier", "broken_tests"])
    assert res.exit_code == 1
    assert "Plugin tests failed" in res.output


def test_plugins_test_order_capable_manifest_only_never_imported(
    runner: CliRunner, make_project: Any
) -> None:
    """An order-capable plugin gets manifest-only check and is never imported."""
    proj = make_project("p_order_capable")
    p_dir = proj / "plugins" / "venue" / "zerodha_live"
    p_dir.mkdir(parents=True)
    (p_dir / "plugin.toml").write_text(
        'name = "zerodha_live"\n'
        'kind = "venue"\n'
        'version = "1.0.0"\n'
        "api_version = 1\n"
        'entry = "plugin:ZerodhaLiveVenue"\n'
        "order_capable = true\n"
        'modes_allowed = ["live"]\n'
        "\n[capabilities]\n"
        "orders = true\n"
    )
    (p_dir / "README.md").write_text("# Live Venue\n\n## Summary\nLive venue summary.\n")
    (p_dir / "tests").mkdir()
    (p_dir / "tests" / "test_smoke.py").write_text("def test_smoke(): pass\n")
    # Write intentionally invalid python code that would fail syntax or raise on import
    (p_dir / "plugin.py").write_text("THIS IS SYNTAX ERROR THAT CANNOT BE IMPORTED %%%%")

    # Order-capable check must pass without attempting to import plugin.py
    res = runner.invoke(app, ["--root", str(proj), "plugins", "test", "venue", "zerodha_live"])
    assert res.exit_code == 0
    assert "passed conformance suite 'base+plugin-tests'" in res.output


def test_order_capable_unit_tests_run_but_registry_never_creates_outside_live(
    runner: CliRunner, make_project: Any
) -> None:
    """R-14 / INV-1: plugins test runs unit tests; registry refuses create outside live."""
    from algodesk.api.errors import SafetyViolation
    from algodesk.api.kinds import Mode
    from algodesk.ops.context import build_context
    from algodesk.ops.runtime import open_runtime

    proj = make_project("p_order_safety")
    p_dir = proj / "plugins" / "venue" / "live_trader"
    p_dir.mkdir(parents=True)
    (p_dir / "plugin.toml").write_text(
        'name = "live_trader"\n'
        'kind = "venue"\n'
        'version = "1.0.0"\n'
        "api_version = 1\n"
        'entry = "plugin:LiveTraderVenue"\n'
        "order_capable = true\n"
        'modes_allowed = ["live"]\n'
        "\n[capabilities]\n"
        "orders = true\n"
    )
    (p_dir / "README.md").write_text("# Live Venue\n\n## Summary\nLive venue summary.\n")
    (p_dir / "tests").mkdir()
    (p_dir / "tests" / "test_units.py").write_text("def test_fake_order(): assert 1 + 1 == 2\n")
    (p_dir / "plugin.py").write_text(
        "from algodesk.api.plugin import PluginBase\n\n"
        "class LiveTraderVenue(PluginBase):\n"
        "    pass\n"
    )

    # 1. plugins test runs the plugin's own unit tests and records conformance
    res = runner.invoke(app, ["--root", str(proj), "plugins", "test", "venue", "live_trader"])
    assert res.exit_code == 0
    assert "passed conformance suite 'base+plugin-tests'" in res.output

    # 2. Registry never creates order-capable plugin outside live
    with open_runtime(root=proj) as rt:
        for mode in (Mode.RESEARCH, Mode.BACKTEST, Mode.PAPER):
            ctx = build_context(mode, run_id=f"test_{mode.value}")
            with pytest.raises(SafetyViolation) as exc_info:
                rt.plugins.create("venue", "live_trader", ctx=ctx)
            assert "cannot be loaded in mode" in exc_info.value.what
            assert "only permitted in 'live' mode" in exc_info.value.what


def test_plugins_test_all_flag(runner: CliRunner, make_project: Any) -> None:
    """plugins test --all discovers and tests all plugins in project."""
    proj = make_project("p_test_all")
    runner.invoke(app, ["--root", str(proj), "new", "plugin", "notifier", "slack"])
    runner.invoke(app, ["--root", str(proj), "new", "plugin", "notifier", "discord"])

    res = runner.invoke(app, ["--root", str(proj), "plugins", "test", "--all"])
    assert res.exit_code == 0
    assert "notifier/slack passed conformance suite" in res.output
    assert "notifier/discord passed conformance suite" in res.output


def test_plugins_test_all_run_flag(runner: CliRunner, tmp_project: Path) -> None:
    """plugins test --all --run NAME runs conformance for all run config plugins."""
    args = [
        "--root",
        str(tmp_project),
        "plugins",
        "test",
        "--all",
        "--run",
        "demo_paper",
        "--force",
    ]
    res = runner.invoke(app, args)
    assert res.exit_code == 0
    assert "broker/upstox passed conformance suite" in res.output
    assert "venue/sim passed conformance suite" in res.output
    assert "fees/zerodha_nse passed conformance suite" in res.output


def test_plugins_test_missing_args_error(runner: CliRunner, make_project: Any) -> None:
    """plugins test without args or --all prints error and exits 1."""
    proj = make_project("p_missing_args")
    res = runner.invoke(app, ["--root", str(proj), "plugins", "test"])
    assert res.exit_code == 1
    assert "specify plugin kind and name, or use '--all'" in res.output
