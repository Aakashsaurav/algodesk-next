"""Retention boundary and interval parameter validation tests for yfinance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from plugins.brokers.yfinance.config_schema import YFinanceConfig
from plugins.brokers.yfinance.plugin import YFinanceBrokerPlugin

from algodesk.api import ConfigError, InstrumentId, PluginManifest
from algodesk.api.kinds import Mode
from algodesk.ops.context import build_context


@pytest.fixture
def fake_manifest() -> PluginManifest:
    return PluginManifest(
        name="yfinance",
        kind="broker",
        version="0.1.0",
        api_version=1,
        entry="plugin:YFinanceBrokerPlugin",
        capabilities={
            "history": True,
            "actions": True,
            "fundamentals": True,
            "info": True,
            "options": True,
            "stream": False,
            "orders": False,
        },
        modes_allowed=["research", "backtest"],
    )


@pytest.mark.parametrize("days_back", [1, 2, 3, 5, 6, 7])
def test_yfinance_1m_retention_valid(days_back: int, fake_manifest: PluginManifest) -> None:
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx)
    now = datetime.now(UTC)
    start = now - timedelta(days=days_back)
    # Should not raise
    plugin._check_retention("1m", start)


@pytest.mark.parametrize("days_back", [8, 9, 10, 14, 30, 60])
def test_yfinance_1m_retention_invalid(days_back: int, fake_manifest: PluginManifest) -> None:
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx)
    now = datetime.now(UTC)
    start = now - timedelta(days=days_back)
    with pytest.raises(ConfigError) as exc_info:
        plugin._check_retention("1m", start)
    assert "exceeds retention" in exc_info.value.what


@pytest.mark.parametrize("interval", ["2m", "5m", "15m", "30m"])
@pytest.mark.parametrize("days_back", [10, 30, 50, 59])
def test_yfinance_intraday_retention_valid(
    interval: str, days_back: int, fake_manifest: PluginManifest
) -> None:
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx)
    now = datetime.now(UTC)
    start = now - timedelta(days=days_back)
    plugin._check_retention(interval, start)


@pytest.mark.parametrize("interval", ["2m", "5m", "15m", "30m"])
@pytest.mark.parametrize("days_back", [61, 70, 90])
def test_yfinance_intraday_retention_invalid(
    interval: str, days_back: int, fake_manifest: PluginManifest
) -> None:
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx)
    now = datetime.now(UTC)
    start = now - timedelta(days=days_back)
    with pytest.raises(ConfigError) as exc_info:
        plugin._check_retention(interval, start)
    assert "exceeds retention" in exc_info.value.what


@pytest.mark.parametrize("days_back", [100, 300, 500, 700])
def test_yfinance_1h_retention_valid(days_back: int, fake_manifest: PluginManifest) -> None:
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx)
    now = datetime.now(UTC)
    start = now - timedelta(days=days_back)
    plugin._check_retention("1h", start)


@pytest.mark.parametrize("days_back", [735, 800, 1000])
def test_yfinance_1h_retention_invalid(days_back: int, fake_manifest: PluginManifest) -> None:
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx)
    now = datetime.now(UTC)
    start = now - timedelta(days=days_back)
    with pytest.raises(ConfigError) as exc_info:
        plugin._check_retention("1h", start)
    assert "exceeds retention" in exc_info.value.what


@pytest.mark.parametrize(
    "symbol_input,expected_ticker",
    [
        ("RELIANCE", "RELIANCE.NS"),
        ("TCS", "TCS.NS"),
        ("INFY", "INFY.NS"),
        ("HDFCBANK", "HDFCBANK.NS"),
        ("SBIN", "SBIN.NS"),
        ("RELIANCE.NS", "RELIANCE.NS"),
        ("TCS.BO", "TCS.BO"),
        (InstrumentId("NSE:EQ:RELIANCE"), "RELIANCE.NS"),
        (InstrumentId("NSE:EQ:TCS"), "TCS.NS"),
        (InstrumentId("NSE:EQ:INFY"), "INFY.NS"),
        (InstrumentId("BSE:EQ:500325"), "500325.BO"),
        (InstrumentId("BSE:EQ:532540"), "532540.BO"),
        (InstrumentId("NYSE:EQ:AAPL"), "AAPL.NYSE"),
        (InstrumentId("NASDAQ:EQ:MSFT"), "MSFT.NASDAQ"),
    ],
)
def test_yfinance_symbol_mapping_parameterized(
    symbol_input: str | InstrumentId, expected_ticker: str, fake_manifest: PluginManifest
) -> None:
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx)
    assert plugin.map_symbol(symbol_input) == expected_ticker
