"""Yahoo Finance DataSource broker plugin implementation."""

from __future__ import annotations

import random
import sys
import time
import warnings
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

import yfinance as yf
from algodesk.api import (
    ActionRecord,
    Bar,
    ConfigError,
    DataSource,
    Instrument,
    InstrumentId,
    MarketEvent,
    NotSupported,
    PluginBase,
    PluginContext,
    PluginManifest,
    SeriesKey,
)

from .config_schema import YFinanceConfig


class YFinanceBrokerPlugin(PluginBase, DataSource):
    """DataSource plugin reading historical bars from Yahoo Finance."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: YFinanceConfig | dict[str, Any] | None = None,
        ctx: PluginContext | None = None,
        *,
        _yf_module: Any = None,
    ) -> None:
        if isinstance(config, YFinanceConfig):
            self._cfg = config
            raw_config: Mapping[str, object] = config.model_dump()
        elif isinstance(config, Mapping):
            self._cfg = YFinanceConfig.model_validate(config)
            raw_config = config
        else:
            self._cfg = YFinanceConfig()
            raw_config = self._cfg.model_dump()

        super().__init__(manifest, raw_config, cast(PluginContext, ctx))
        self._yf = _yf_module if _yf_module is not None else yf

    def _param(self, name: str, default: Any) -> Any:
        if hasattr(self, "ctx") and self.ctx and self.ctx.params:
            try:
                return self.ctx.params.get(name)
            except Exception:
                return default
        return default

    def map_symbol(self, instrument: InstrumentId | str) -> str:
        """Map canonical InstrumentId or raw symbol to Yahoo Finance ticker."""
        raw = str(instrument).strip()
        if ":" in raw:
            parts = raw.split(":")
            exch = parts[0].upper()
            sym = parts[2] if len(parts) >= 3 else parts[-1]
            if exch == "NSE":
                return f"{sym}.NS"
            if exch == "BSE":
                return f"{sym}.BO"
            return f"{sym}.{exch}"

        if raw.endswith(".NS") or raw.endswith(".BO"):
            return raw
        exch_default = self._cfg.default_exchange.upper()
        suffix = ".BO" if exch_default == "BSE" else ".NS"
        return f"{raw}{suffix}"

    def _check_retention(self, interval: str, start: datetime) -> None:
        now = datetime.now(UTC)
        start_utc = start if start.tzinfo is not None else start.replace(tzinfo=UTC)
        age_days = (now - start_utc).days

        ret_1m = int(self._param("yfinance.retention_1m_days", 7))
        ret_intraday = int(self._param("yfinance.retention_intraday_days", 60))
        ret_1h = int(self._param("yfinance.retention_1h_days", 730))

        if interval == "1m" and age_days > ret_1m:
            raise ConfigError(
                what=(
                    f"1m timeframe requested start {age_days} days ago, exceeds retention {ret_1m}d"
                ),
                where=f"interval={interval}",
                fix=f"Select start date within last {ret_1m} days or use timeframe >= 5m",
            )
        if interval in ("2m", "5m", "15m", "30m", "60m", "90m") and age_days > ret_intraday:
            raise ConfigError(
                what=(
                    f"{interval} interval requested {age_days} days ago "
                    f"exceeds retention {ret_intraday}d"
                ),
                where=f"interval={interval}",
                fix=f"Select start date within last {ret_intraday} days or use 1d timeframe",
            )
        if interval in ("1h", "60m") and age_days > ret_1h:
            raise ConfigError(
                what=f"1h interval requested {age_days} days ago exceeds retention {ret_1h}d",
                where=f"interval={interval}",
                fix=f"Select start date within last {ret_1h} days or use 1d timeframe",
            )

    def _execute_with_retry(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        max_retries = int(self._param("yfinance.max_retries", 3))
        base_s = float(self._param("yfinance.backoff_base_s", 1.0))
        jitter_s = float(self._param("yfinance.backoff_jitter_s", 0.5))

        for attempt in range(max_retries + 1):
            try:
                with warnings.catch_warnings():
                    pandas_errors = sys.modules.get("pandas.errors")
                    if pandas_errors is not None:
                        p4w = getattr(pandas_errors, "Pandas4Warning", None)
                        if p4w is not None and issubclass(p4w, Warning):
                            warnings.filterwarnings("ignore", category=p4w)
                    warnings.filterwarnings("ignore", message=".*Pandas4Warning.*")
                    warnings.filterwarnings("ignore", message=".*pandas 4.*")
                    return fn(*args, **kwargs)
            except Exception as exc:
                is_429 = "429" in str(exc) or "Too Many Requests" in str(exc)
                if is_429 and attempt < max_retries:
                    delay = base_s * (2**attempt) + random.uniform(0.0, jitter_s)
                    time.sleep(delay)
                    continue
                raise

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        """Fetch historical bars from Yahoo Finance for the specified SeriesKey."""
        self.require("history")
        sym = self.map_symbol(key.instrument)
        tf = key.timeframe.lower()
        valid_intervals = {"1m", "5m", "15m", "30m", "1h", "1d"}
        if tf not in valid_intervals:
            raise ConfigError(
                what=f"Unsupported interval '{tf}' for yfinance",
                where="yfinance.history",
                fix=f"Use one of supported intervals: {sorted(valid_intervals)}",
            )

        self._check_retention(tf, start)
        is_adjusted = str(key.adjustment).lower() in ("adjusted", "adjustment.adjusted")

        start_str = start.strftime("%Y-%m-%d")
        end_str = (
            (end + timedelta(days=1)).strftime("%Y-%m-%d")
            if tf == "1d"
            else end.strftime("%Y-%m-%d")
        )

        def _fetch() -> Any:
            ticker = self._yf.Ticker(sym)
            return ticker.history(
                start=start_str,
                end=end_str,
                interval=tf,
                auto_adjust=is_adjusted,
            )

        df = self._execute_with_retry(_fetch)
        if df is None or len(df) == 0:
            return []

        bars: list[Bar] = []
        for idx_ts, row in df.iterrows():
            ts = idx_ts.to_pydatetime() if hasattr(idx_ts, "to_pydatetime") else idx_ts
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            else:
                ts = ts.astimezone(UTC)

            # Check within requested boundary
            if tf == "1d":
                ist = ZoneInfo("Asia/Kolkata")
                bar_date = ts.astimezone(ist).date()
                s_date = start.astimezone(ist).date() if start.tzinfo else start.date()
                e_date = end.astimezone(ist).date() if end.tzinfo else end.date()
                if bar_date < s_date or bar_date > e_date:
                    continue
            else:
                if ts < start or ts > end:
                    continue

            b = Bar(
                instrument=key.instrument,
                timeframe=key.timeframe,
                start_ts=ts,
                end_ts=ts,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=int(row["Volume"]),
                complete=True,
            )
            bars.append(b)
        return bars

    def instruments(self) -> Iterable[Instrument]:
        """Listing instrument master is not supported by yfinance."""
        raise NotSupported(
            what="Yahoo Finance does not provide an official tradable instrument master catalog",
            where="YFinanceBrokerPlugin.instruments",
            fix="Use an exchange broker plugin (e.g. upstox) for instrument master catalogs",
        )

    def stream(self, instruments: Sequence[InstrumentId]) -> AsyncIterator[MarketEvent]:
        """Streaming real-time market data is not supported by yfinance."""
        raise NotSupported(
            what="Yahoo Finance does not support real-time WebSocket streaming",
            where="YFinanceBrokerPlugin.stream",
            fix="Use live suitable broker plugin (e.g. upstox) for streaming market events",
        )

    def actions(self, symbol: str | InstrumentId) -> Sequence[ActionRecord]:
        """Fetch plain corporate actions data (dividends and splits)."""
        self.require("corporate_actions")
        sym = self.map_symbol(symbol)
        clean_sym = sym.split(".")[0]
        if sym.endswith(".NS"):
            exch = "NSE"
        elif sym.endswith(".BO"):
            exch = "BSE"
        else:
            exch = self._cfg.default_exchange.upper()

        def _fetch() -> Any:
            ticker = self._yf.Ticker(sym)
            return ticker.actions

        df = self._execute_with_retry(_fetch)
        if df is None or len(df) == 0:
            return []

        now_iso = datetime.now(UTC).isoformat()
        records: list[ActionRecord] = []
        for idx_ts, row in df.iterrows():
            ts = idx_ts.to_pydatetime() if hasattr(idx_ts, "to_pydatetime") else idx_ts
            ex_d = ts.date() if hasattr(ts, "date") else ts

            if "Stock Splits" in df.columns:
                split_val = float(row.get("Stock Splits") or 0.0)
                if split_val > 0.0:
                    records.append(
                        ActionRecord(
                            ex_date=ex_d,
                            type="split",
                            ratio=split_val,
                            source="yfinance",
                            source_ref=sym,
                            fetched_at=now_iso,
                            symbol=clean_sym,
                            exchange=exch,
                            status="unverified",
                        )
                    )

            if "Dividends" in df.columns:
                div_val = float(row.get("Dividends") or 0.0)
                if div_val > 0.0:
                    records.append(
                        ActionRecord(
                            ex_date=ex_d,
                            type="dividend",
                            amount=Decimal(str(row["Dividends"])),
                            source="yfinance",
                            source_ref=sym,
                            fetched_at=now_iso,
                            symbol=clean_sym,
                            exchange=exch,
                            status="unverified",
                        )
                    )

        return sorted(records, key=lambda a: (a.ex_date, a.type))

    def fundamentals(self, symbol: str | InstrumentId) -> Mapping[str, Any]:
        """Fetch financial statement fundamentals."""
        sym = self.map_symbol(symbol)
        financials = self._execute_with_retry(lambda: self._yf.Ticker(sym).financials)
        return financials.to_dict() if hasattr(financials, "to_dict") else {}

    def info(self, symbol: str | InstrumentId) -> Mapping[str, Any]:
        """Fetch general company profile info."""
        sym = self.map_symbol(symbol)
        return self._execute_with_retry(lambda: self._yf.Ticker(sym).info) or {}

    def options_chain(self, symbol: str | InstrumentId) -> Mapping[str, Any]:
        """Fetch options chain expiration dates and strikes."""
        sym = self.map_symbol(symbol)
        ticker = self._execute_with_retry(lambda: self._yf.Ticker(sym))
        expirations = self._execute_with_retry(lambda: ticker.options) or []
        res: dict[str, Any] = {"expirations": list(expirations), "chains": {}}
        if expirations:
            first_exp = expirations[0]
            chain = self._execute_with_retry(lambda: ticker.option_chain(first_exp))
            if chain:
                calls = (
                    chain.calls.to_dict(orient="records") if hasattr(chain.calls, "to_dict") else []
                )
                puts = (
                    chain.puts.to_dict(orient="records") if hasattr(chain.puts, "to_dict") else []
                )
                res["chains"][first_exp] = {"calls": calls, "puts": puts}
        return res
