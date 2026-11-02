"""Core unit tests for Yahoo Finance broker DataSource plugin."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from plugins.brokers.yfinance.config_schema import YFinanceConfig
from plugins.brokers.yfinance.plugin import YFinanceBrokerPlugin

from algodesk.api import (
    ConfigError,
    InstrumentId,
    NotSupported,
    PluginManifest,
    SeriesKey,
    data_source_problems,
)
from algodesk.api.kinds import Mode
from algodesk.ops.context import build_context


class FakeSeries:
    def __init__(self, data: list[float] | None = None) -> None:
        self._data = data or [100.0, 105.0, 95.0, 102.0, 1000.0]

    def tolist(self) -> list[float]:
        return self._data

    def __iter__(self) -> Any:
        return iter(self._data)


class FakeDataFrame:
    def __init__(self, records: list[dict[str, Any]], dates: list[datetime]) -> None:
        self._records = records
        self._dates = dates

    def __len__(self) -> int:
        return len(self._records)

    @property
    def columns(self) -> list[str]:
        if not self._records:
            return []
        return list(self._records[0].keys())

    def iterrows(self) -> Any:
        yield from zip(self._dates, self._records, strict=False)

    def to_dict(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"records": self._records}


class FakeOptionChain:
    def __init__(self) -> None:
        self.calls = FakeDataFrame([{"strike": 2500, "lastPrice": 50}], [datetime.now(UTC)])
        self.puts = FakeDataFrame([{"strike": 2400, "lastPrice": 30}], [datetime.now(UTC)])


class FakeTicker:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.actions = FakeDataFrame([{"Dividends": 10.0}], [datetime.now(UTC)])
        self.financials = FakeDataFrame([{"TotalRevenue": 1000000}], [datetime.now(UTC)])
        self.info = {"symbol": symbol, "sector": "Energy", "shortName": "Reliance"}
        self.options = ["2026-11-26", "2026-12-31"]

    def history(
        self,
        start: str | datetime,
        end: str | datetime,
        interval: str,
        auto_adjust: bool = False,
    ) -> FakeDataFrame:
        now = datetime.now(UTC)
        close_price = 2530.0 if auto_adjust else 2540.0
        records = [
            {
                "Open": 2500.0,
                "High": 2550.0,
                "Low": 2490.0,
                "Close": close_price,
                "Adj Close": 2530.0,
                "Volume": 10000,
            }
        ]
        dates = [now - timedelta(days=1)]
        return FakeDataFrame(records, dates)

    def option_chain(self, date: str) -> FakeOptionChain:
        return FakeOptionChain()


class FakeYFinanceModule:
    def __init__(self) -> None:
        self.fail_429_count = 0
        self.calls_count = 0

    def Ticker(self, symbol: str) -> FakeTicker:
        self.calls_count += 1
        if self.fail_429_count > 0:
            self.fail_429_count -= 1
            raise Exception("HTTP 429 Client Error: Too Many Requests")
        return FakeTicker(symbol)


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
            "corporate_actions": True,
            "actions": True,
            "fundamentals": True,
            "info": True,
            "options": True,
            "stream": False,
            "orders": False,
        },
        modes_allowed=["research", "backtest"],
    )


def test_yfinance_symbol_mapping(fake_manifest: PluginManifest) -> None:
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx)

    assert plugin.map_symbol("RELIANCE") == "RELIANCE.NS"
    assert plugin.map_symbol(InstrumentId("NSE:EQ:INFY")) == "INFY.NS"
    assert plugin.map_symbol(InstrumentId("BSE:EQ:500325")) == "500325.BO"
    assert plugin.map_symbol(InstrumentId("NYSE:EQ:AAPL")) == "AAPL.NYSE"


def test_yfinance_raw_vs_adjusted(fake_manifest: PluginManifest) -> None:
    fake_mod = FakeYFinanceModule()
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx, _yf_module=fake_mod)

    raw_key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="yfinance",
    )
    adj_key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="adjusted",
        source="yfinance",
    )

    now = datetime.now(UTC)
    raw_bars = list(plugin.history(raw_key, now - timedelta(days=5), now))
    assert len(raw_bars) == 1
    assert raw_bars[0].close == 2540.0

    adj_bars = list(plugin.history(adj_key, now - timedelta(days=5), now))
    assert len(adj_bars) == 1
    assert adj_bars[0].close == 2530.0


def test_yfinance_retention_checks(fake_manifest: PluginManifest) -> None:
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx)
    now = datetime.now(UTC)

    # 1m older than 7 days raises ConfigError
    key_1m = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="yfinance",
    )
    with pytest.raises(ConfigError) as exc_1m:
        list(plugin.history(key_1m, now - timedelta(days=10), now))
    assert "exceeds retention" in exc_1m.value.what

    # 5m older than 60 days raises ConfigError
    key_5m = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="5m",
        adjustment="raw",
        source="yfinance",
    )
    with pytest.raises(ConfigError) as exc_5m:
        list(plugin.history(key_5m, now - timedelta(days=70), now))
    assert "exceeds retention" in exc_5m.value.what

    # 1h older than 730 days raises ConfigError
    key_1h = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1h",
        adjustment="raw",
        source="yfinance",
    )
    with pytest.raises(ConfigError) as exc_1h:
        list(plugin.history(key_1h, now - timedelta(days=800), now))
    assert "exceeds retention" in exc_1h.value.what


def test_yfinance_http_429_retry_jitter(fake_manifest: PluginManifest) -> None:
    fake_mod = FakeYFinanceModule()
    fake_mod.fail_429_count = 2  # Fail twice with 429, then succeed
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx, _yf_module=fake_mod)

    info = plugin.info("RELIANCE")
    assert info["symbol"] == "RELIANCE.NS"
    assert fake_mod.fail_429_count == 0


def test_yfinance_not_supported_methods(fake_manifest: PluginManifest) -> None:
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx)

    with pytest.raises(NotSupported):
        plugin.instruments()

    with pytest.raises(NotSupported):
        plugin.stream([InstrumentId("NSE:EQ:RELIANCE")])


def test_yfinance_extra_methods(fake_manifest: PluginManifest) -> None:
    fake_mod = FakeYFinanceModule()
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx, _yf_module=fake_mod)

    actions = plugin.actions("RELIANCE")
    assert len(actions) == 1
    assert actions[0].type == "dividend"

    fund = plugin.fundamentals("RELIANCE")
    assert "records" in fund

    chain = plugin.options_chain("RELIANCE")
    assert "expirations" in chain
    assert len(chain["expirations"]) == 2


def test_yfinance_conformance(fake_manifest: PluginManifest) -> None:
    ctx = build_context(Mode.RESEARCH)
    plugin = YFinanceBrokerPlugin(fake_manifest, YFinanceConfig(), ctx)
    problems = data_source_problems(plugin)
    assert problems == []
