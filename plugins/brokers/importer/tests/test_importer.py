"""Conformance and functionality tests for importer broker plugin."""

from __future__ import annotations

from datetime import UTC

import pytest

from algodesk.api import (
    InstrumentId,
    NotSupported,
    PluginManifest,
    SeriesKey,
    data_source_problems,
)

from ..plugin import ImporterBrokerPlugin


@pytest.fixture
def manifest() -> PluginManifest:
    return PluginManifest(
        name="importer",
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:ImporterBrokerPlugin",
        capabilities={
            "history": True,
            "stream": False,
            "realtime": False,
            "live_suitable": False,
            "official_api": False,
        },
    )


def _make_plugin(m: PluginManifest) -> ImporterBrokerPlugin:
    return ImporterBrokerPlugin(manifest=m, config={})


def test_importer_conformance(manifest: PluginManifest) -> None:
    plugin = _make_plugin(manifest)
    assert data_source_problems(plugin) == []


def test_importer_instruments_unsupported(manifest: PluginManifest) -> None:
    plugin = _make_plugin(manifest)
    with pytest.raises(NotSupported) as exc:
        plugin.instruments()
    assert "instrument catalog" in exc.value.what


def test_importer_stream_unsupported(manifest: PluginManifest) -> None:
    plugin = _make_plugin(manifest)
    with pytest.raises(NotSupported) as exc:
        plugin.stream([InstrumentId("NSE:EQ:RELIANCE")])
    assert "streaming" in exc.value.what


def test_importer_history_call(manifest: PluginManifest) -> None:
    plugin = _make_plugin(manifest)
    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="importer",
    )
    from datetime import datetime

    dt = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
    bars = list(plugin.history(key, dt, dt))
    assert bars == []
