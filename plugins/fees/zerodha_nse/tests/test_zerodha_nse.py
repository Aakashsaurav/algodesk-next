"""Tests for ZerodhaNseFeesPlugin."""

from __future__ import annotations

from algodesk.api.manifest import PluginManifest

from ..plugin import ZerodhaNseFeesPlugin


def test_zerodha_nse_schedule_loading() -> None:
    manifest = PluginManifest(
        name="zerodha_nse",
        kind="fees",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:ZerodhaNseFeesPlugin",
        capabilities={"equity_delivery": True, "equity_intraday": True, "futures": True},
    )
    plugin = ZerodhaNseFeesPlugin(manifest)
    sch = plugin.schedule()

    assert sch.meta["name"] == "zerodha_nse"
    assert len(sch.components) >= 10
    assert len(sch.taxes) >= 1

    # Check mapping access
    assert sch["meta"]["name"] == "zerodha_nse"
