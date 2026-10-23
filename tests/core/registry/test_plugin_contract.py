"""Tests for registry.base_checks plugin_contract_problems calling kind_problems."""

from __future__ import annotations

from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.plugin import PluginBase
from algodesk.api.protocols import Message
from algodesk.registry.base_checks import plugin_contract_problems


def _make_manifest(kind: PluginKind) -> PluginManifest:
    return PluginManifest(
        name="test_p",
        kind=kind,
        version="0.1.0",
        api_version=1,
        entry="plugin:TestPlugin",
        capabilities={"send": True},
    )


class FullNotifierPlugin(PluginBase):
    def send(self, message: Message) -> None:
        pass


class IncompleteNotifierPlugin(PluginBase):
    # Misses send()
    pass


def test_plugin_contract_problems_conforming() -> None:
    manifest = _make_manifest(PluginKind.NOTIFIER)
    plugin = FullNotifierPlugin(manifest, {}, None)  # type: ignore[arg-type]
    problems = plugin_contract_problems(plugin)
    assert problems == []


def test_plugin_contract_problems_kind_failure() -> None:
    manifest = _make_manifest(PluginKind.NOTIFIER)
    plugin = IncompleteNotifierPlugin(manifest, {}, None)  # type: ignore[arg-type]
    problems = plugin_contract_problems(plugin)
    assert any("send" in p for p in problems)
