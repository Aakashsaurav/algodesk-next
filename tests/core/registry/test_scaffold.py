"""Tests for plugin scaffolding utility."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def test_scaffold_creates_valid_structure(tmp_path: Path) -> None:
    from algodesk.registry.scaffold import scaffold_plugin

    dest = tmp_path / "plugins"
    p_dir = scaffold_plugin(dest, "notifier", "slack")
    assert p_dir.is_dir()
    assert (p_dir / "plugin.toml").is_file()
    assert (p_dir / "README.md").is_file()
    assert (p_dir / "plugin.py").is_file()
    assert (p_dir / "tests").is_dir()
    assert (p_dir / "tests" / "test_slack.py").is_file()


def test_scaffold_passes_discovery(tmp_path: Path) -> None:
    from algodesk.registry.discovery import discover
    from algodesk.registry.scaffold import scaffold_plugin

    dest = tmp_path / "plugins"
    scaffold_plugin(dest, "venue", "fake_exchange")

    plugins, issues = discover([dest])
    assert len(issues) == 0
    assert len(plugins) == 1
    assert plugins[0].name == "fake_exchange"
    assert plugins[0].kind.value == "venue"


def test_scaffold_refuses_to_overwrite_non_empty(tmp_path: Path) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.registry.scaffold import scaffold_plugin

    dest = tmp_path / "plugins"
    scaffold_plugin(dest, "notifier", "repeat")

    with pytest.raises(PluginError, match="already exists"):
        scaffold_plugin(dest, "notifier", "repeat")


def test_scaffold_overwrites_empty_dir(tmp_path: Path) -> None:
    from algodesk.registry.scaffold import scaffold_plugin

    target = tmp_path / "plugins" / "notifier" / "empty_target"
    target.mkdir(parents=True, exist_ok=True)
    p_dir = scaffold_plugin(tmp_path / "plugins", "notifier", "empty_target")
    assert (p_dir / "plugin.toml").is_file()


def test_scaffold_loaded_and_conforms(
    tmp_path: Path,
    context_factory: Any,
) -> None:
    from algodesk.api.kinds import Mode
    from algodesk.registry.base_checks import base_contract_problems
    from algodesk.registry.registry import PluginRegistry
    from algodesk.registry.scaffold import scaffold_plugin

    dest = tmp_path / "plugins"
    scaffold_plugin(dest, "notifier", "live_demo")
    reg = PluginRegistry(tmp_path, [dest], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    plugin = reg.create("notifier", "live_demo", ctx=ctx)
    problems = base_contract_problems(plugin)
    assert problems == []
