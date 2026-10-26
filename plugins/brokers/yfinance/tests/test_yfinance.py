"""Tests for YFinanceBrokerPlugin using a mocked yfinance module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from algodesk.api import (
    ConfigError,
    InstrumentId,
    NotSupported,
    PluginManifest,
    SeriesKey,
    data_source_problems,
)

from ..config_schema import YFinanceConfig
from ..plugin import YFinanceBrokerPlugin


class FakeTable:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.empty = len(records) == 0

    def to_dict(self, orient: str = "records") -> list[dict[str, Any]]:
        return self._records


class FakeDataFrame:
    def __init__(self, records: list[dict[str, Any]], dates: list[datetime]) -> None:
        self._records = records
        self._dates = dates

    def __len__(self) -> int:
        return len(self._records)

    def iterrows(self) -> Any:
        yield from zip(self._dates, self._records, strict=False)


class FakeOptionChain:
    def __init__(self) -> None:
        self.calls = FakeTable([{"strike": 2500, "lastPrice": 50.0}])
        self.puts = FakeTable([{"strike": 2500, "lastPrice": 45.0}])


class FakeTicker:
    def __init__(self, symbol: str, fail_429_count: int = 0) -> None:
        self.symbol = symbol
        self.fail_429_count = fail_429_count
        self.history_calls: list[dict[str, Any]] = []
        self.actions = FakeTable([{"Dividends": 10.0, "Stock Splits": 0.0}])
        self.financials = FakeTable([{"Revenue": 1000000}])
        self.info = {"shortName": "Reliance Industries", "symbol": symbol}
        self.options = ["2026-10-29", "2026-11-26"]

    def history(
        self,
        start: str,
        end: str,
        interval: str,
        auto_adjust: bool = False,
    ) -> FakeDataFrame:
        self.history_calls.append(
            {"start": start, "end": end, "interval": interval, "auto_adjust": auto_adjust}
        )
        if self.fail_429_count > 0:
            self.fail_429_count -= 1
            raise RuntimeError("HTTP 429 Client Error: Too Many Requests")

        s_dt = datetime.fromisoformat(start)
        if s_dt.tzinfo is None:
            s_dt = s_dt.replace(tzinfo=UTC)
        dates = [s_dt + timedelta(days=i) for i in range(3)]
        records = [
            {"Open": 100.0, "High": 105.0, "Low": 99.0, "Close": 102.0, "Volume": 1000},
            {"Open": 102.0, "High": 106.0, "Low": 101.0, "Close": 103.0, "Volume": 1500},
            {"Open": 101.0, "High": 104.0, "Low": 100.0, "Close": 102.5, "Volume": 1200},
        ]
        return FakeDataFrame(records, dates)

    def option_chain(self, date: str) -> FakeOptionChain:
        return FakeOptionChain()


class FakeYFModule:
    def __init__(self, fail_429_count: int = 0) -> None:
        self.fail_429_count = fail_429_count
        self.tickers: dict[str, FakeTicker] = {}

    def Ticker(self, symbol: str) -> FakeTicker:
        if symbol not in self.tickers:
            self.tickers[symbol] = FakeTicker(symbol, fail_429_count=self.fail_429_count)
        return self.tickers[symbol]


@pytest.fixture
def manifest() -> PluginManifest:
    return PluginManifest(
        name="yfinance",
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:YFinanceBrokerPlugin",
        capabilities={
            "history": True,
            "stream": False,
            "realtime": False,
            "live_suitable": False,
            "official_api": False,
        },
    )


def test_yfinance_symbol_mapping(manifest: PluginManifest) -> None:
    plugin = YFinanceBrokerPlugin(manifest)
    assert plugin.map_symbol(InstrumentId("NSE:EQ:RELIANCE")) == "RELIANCE.NS"
    assert plugin.map_symbol(InstrumentId("BSE:EQ:RELIANCE")) == "RELIANCE.BO"
    assert plugin.map_symbol("TCS") == "TCS.NS"
    assert plugin.map_symbol("INFY.BO") == "INFY.BO"

    bse_cfg = YFinanceConfig(default_exchange="BSE")
    bse_plugin = YFinanceBrokerPlugin(manifest, config=bse_cfg)
    assert bse_plugin.map_symbol("TCS") == "TCS.BO"


def test_yfinance_history_raw_and_adjusted(manifest: PluginManifest) -> None:
    fake_yf = FakeYFModule()
    plugin = YFinanceBrokerPlugin(manifest, _yf_module=fake_yf)

    key_raw = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="yfinance",
    )
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 5, tzinfo=UTC)

    bars_raw = list(plugin.history(key_raw, start, end))
    assert len(bars_raw) == 3
    assert bars_raw[0].open == 100.0
    assert bars_raw[0].volume == 1000

    ticker = fake_yf.tickers["RELIANCE.NS"]
    assert ticker.history_calls[0]["auto_adjust"] is False

    # Adjusted
    key_adj = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="adjusted",
        source="yfinance",
    )
    list(plugin.history(key_adj, start, end))
    assert ticker.history_calls[1]["auto_adjust"] is True


def test_yfinance_retention_errors(manifest: PluginManifest) -> None:
    plugin = YFinanceBrokerPlugin(manifest, _yf_module=FakeYFModule())
    now = datetime.now(UTC)

    # 1m older than 7 days
    key_1m = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="yfinance",
    )
    with pytest.raises(ConfigError, match="1m timeframe requested"):
        list(plugin.history(key_1m, now - timedelta(days=15), now))

    # 5m older than 60 days
    key_5m = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="5m",
        adjustment="raw",
        source="yfinance",
    )
    with pytest.raises(ConfigError, match="5m interval requested"):
        list(plugin.history(key_5m, now - timedelta(days=90), now))

    # 1h older than 730 days
    key_1h = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1h",
        adjustment="raw",
        source="yfinance",
    )
    with pytest.raises(ConfigError, match="1h interval requested"):
        list(plugin.history(key_1h, now - timedelta(days=800), now))


def test_yfinance_unsupported_methods_raise(manifest: PluginManifest) -> None:
    plugin = YFinanceBrokerPlugin(manifest, _yf_module=FakeYFModule())
    with pytest.raises(NotSupported, match="tradable instrument master"):
        plugin.instruments()

    with pytest.raises(NotSupported, match="WebSocket streaming"):
        plugin.stream([InstrumentId("NSE:EQ:RELIANCE")])


def test_yfinance_429_backoff_and_retry(manifest: PluginManifest) -> None:
    fake_yf = FakeYFModule(fail_429_count=1)
    plugin = YFinanceBrokerPlugin(manifest, _yf_module=fake_yf)

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="yfinance",
    )
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 5, tzinfo=UTC)

    # Should succeed after 1 retry
    bars = list(plugin.history(key, start, end))
    assert len(bars) == 3


def test_yfinance_extra_methods(manifest: PluginManifest) -> None:
    fake_yf = FakeYFModule()
    plugin = YFinanceBrokerPlugin(manifest, _yf_module=fake_yf)

    actions = plugin.actions("RELIANCE")
    assert len(actions) > 0 and "Dividends" in actions[0]

    fund = plugin.fundamentals("RELIANCE")
    assert len(fund) > 0 and "Revenue" in fund[0]

    info = plugin.info("RELIANCE")
    assert info.get("shortName") == "Reliance Industries"

    chain = plugin.options_chain("RELIANCE")
    assert "2026-10-29" in chain["expirations"]
    assert "calls" in chain["chains"]["2026-10-29"]


def test_yfinance_conformance(manifest: PluginManifest) -> None:
    plugin = YFinanceBrokerPlugin(manifest, _yf_module=FakeYFModule())
    problems = data_source_problems(plugin)
    assert problems == []
