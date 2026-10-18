"""Tests for registry doctor checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def test_registry_doctor_checks_structure(tmp_path: Path) -> None:
    from algodesk.api.checks import Severity
    from algodesk.registry.base_checks import registry_doctor_checks
    from algodesk.registry.registry import PluginRegistry

    reg = PluginRegistry(tmp_path, [tmp_path / "plugins"], state_dir=tmp_path / ".algodesk")
    checks = registry_doctor_checks(reg)
    assert len(checks) == 2
    by_id = {c.id: c for c in checks}
    assert "plugins.manifests_valid" in by_id
    assert "plugins.no_order_capable_outside_live" in by_id
    assert by_id["plugins.manifests_valid"].severity == Severity.WARNING
    assert by_id["plugins.no_order_capable_outside_live"].severity == Severity.CRITICAL


def test_doctor_manifests_valid_passes(
    tmp_path: Path,
    fake_plugin_factory: Any,
) -> None:
    from algodesk.registry.base_checks import registry_doctor_checks
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(plugins_dir, "notifier", "demo")
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    checks = registry_doctor_checks(reg)
    check = next(c for c in checks if c.id == "plugins.manifests_valid")
    result = check.run()
    assert result.ok is True
    assert "valid" in result.message


def test_doctor_manifests_valid_fails(
    tmp_path: Path,
    fake_plugin_factory: Any,
) -> None:
    from algodesk.registry.base_checks import registry_doctor_checks
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(plugins_dir, "notifier", "bad_demo", readme_text="__OMIT__")
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    checks = registry_doctor_checks(reg)
    check = next(c for c in checks if c.id == "plugins.manifests_valid")
    result = check.run()
    assert result.ok is False
    assert "issue(s) detected" in result.message
    assert result.fix is not None


def test_doctor_no_order_capable_outside_live_passes_empty(tmp_path: Path) -> None:
    from algodesk.registry.base_checks import registry_doctor_checks
    from algodesk.registry.registry import PluginRegistry

    reg = PluginRegistry(tmp_path, [tmp_path / "plugins"], state_dir=tmp_path / ".algodesk")
    checks = registry_doctor_checks(reg)
    check = next(c for c in checks if c.id == "plugins.no_order_capable_outside_live")
    result = check.run()
    assert result.ok is True


def test_doctor_no_order_capable_outside_live_passes_live(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.kinds import Mode
    from algodesk.registry.base_checks import registry_doctor_checks
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "venue",
        "live_v",
        order_capable=True,
        modes_allowed=["live"],
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")
    reg.record_conformance("venue", "live_v", "suite")

    ctx = context_factory(Mode.LIVE)
    reg.create("venue", "live_v", ctx=ctx)

    checks = registry_doctor_checks(reg)
    check = next(c for c in checks if c.id == "plugins.no_order_capable_outside_live")
    result = check.run()
    assert result.ok is True


def test_doctor_no_order_capable_outside_live_fails(
    tmp_path: Path,
    fake_plugin_factory: Any,
) -> None:
    from algodesk.api.kinds import Mode, PluginKind
    from algodesk.registry.base_checks import registry_doctor_checks
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "venue",
        "violator",
        order_capable=True,
        modes_allowed=["live"],
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    reg._loaded.append((PluginKind.VENUE, "violator", Mode.PAPER))

    checks = registry_doctor_checks(reg)
    check = next(c for c in checks if c.id == "plugins.no_order_capable_outside_live")
    result = check.run()
    assert result.ok is False
    assert "was loaded in mode 'paper'" in result.message
