"""NSE official Bhavcopy reference plugin implementation."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from algodesk.api.errors import NotSupported
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import (
    ActionRecord,
    Bar,
    Instrument,
    InstrumentId,
    MarketEvent,
    SeriesKey,
)
from algodesk.api.plugin import PluginBase
from algodesk.api.services import PluginContext

from .parser import bhavcopy_row_to_bar, parse_bhavcopy_csv

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_EXCH_TZ = ZoneInfo("Asia/Kolkata")
_UDIFF_CUTOFF = date(2024, 7, 8)


class NSEBhavcopyReferencePlugin(PluginBase):
    """ReferenceSource plugin providing unadjusted raw daily bars from NSE Bhavcopy."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, Any] | None = None,
        ctx: PluginContext | None = None,
        *,
        _transport: httpx.BaseTransport | None = None,
        _fixture_path: Path | str | None = None,
        _data_dir: Path | str | None = None,
        _store: Any | None = None,
        _manifest: Any | None = None,
    ) -> None:
        self.manifest = manifest
        self._cfg = dict(config or {})
        self._ctx = ctx
        self._transport = _transport
        self._fixture_path = Path(_fixture_path) if _fixture_path else None
        self._data_dir = Path(_data_dir) if _data_dir else Path("data")
        self._store = _store
        self._manifest = _manifest

    def _bhavcopy_url(self, d: date) -> str:
        if d >= _UDIFF_CUTOFF:
            d_str = d.strftime("%Y%m%d")
            return f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d_str}_F_0000.csv.zip"
        mon = d.strftime("%b").upper()
        d_str = d.strftime("%d%b%Y").upper()
        return (
            f"https://nsearchives.nseindia.com/content/historical/EQUITIES/"
            f"{d.year}/{mon}/cm{d_str}bhav.csv.zip"
        )

    def _load_from_fixture(self, d: date) -> str | None:
        if not self._fixture_path:
            return None
        d_str = d.strftime("%Y%m%d")
        csv_p = self._fixture_path / f"{d_str}.csv"
        if csv_p.is_file():
            return csv_p.read_text(encoding="utf-8")
        zip_p = self._fixture_path / f"{d_str}.csv.zip"
        if zip_p.is_file():
            with zipfile.ZipFile(zip_p) as zf:
                name = zf.namelist()[0]
                with zf.open(name) as f:
                    return f.read().decode("utf-8", errors="ignore")
        return None

    def _fetch_file_content(self, d: date) -> str | None:
        from_fix = self._load_from_fixture(d)
        if from_fix is not None:
            return from_fix

        cache_dir = self._data_dir / "bhavcopy" / "raw" / str(d.year)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached_zip = cache_dir / f"bhav_{d.strftime('%Y%m%d')}.zip"
        if cached_zip.is_file():
            with zipfile.ZipFile(cached_zip) as zf:
                return zf.open(zf.namelist()[0]).read().decode("utf-8", errors="ignore")

        url = self._bhavcopy_url(d)
        client_kwargs: dict[str, Any] = {
            "headers": _HEADERS,
            "timeout": 15.0,
            "follow_redirects": True,
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        for _ in range(2):
            try:
                with httpx.Client(**client_kwargs) as client:
                    resp = client.get(url)
                    if resp.status_code == 200:
                        cached_zip.write_bytes(resp.content)
                        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                            return zf.open(zf.namelist()[0]).read().decode("utf-8", errors="ignore")
                    if resp.status_code == 404:
                        return None
            except Exception:
                pass
        return None

    def _handle_trading_signals(self, d: date, content: str | None) -> None:
        if self._manifest is None:
            return
        is_weekday = d.weekday() < 5
        day_str = d.isoformat()
        now_d = datetime.now(_EXCH_TZ).date()
        if content is None:
            if is_weekday and d < (now_d - timedelta(days=1)):
                self._manifest.record_market_closure(
                    exchange="NSE",
                    day=day_str,
                    kind="exchange_file",
                    evidence=json.dumps(
                        {"source": "nse_bhavcopy", "status": "absent", "attempts": 2}
                    ),
                )
        else:
            if not is_weekday:
                self._manifest.record_market_closure(
                    exchange="NSE",
                    day=day_str,
                    kind="special",
                    evidence=json.dumps(
                        {"source": "nse_bhavcopy", "status": "file_present", "is_weekend": True}
                    ),
                )

    def daily_bars(
        self,
        symbols_or_isins: Sequence[str],
        start: date,
        end: date,
    ) -> Sequence[Bar]:
        """Fetch and store daily bars for requested symbols/ISINs across date range."""
        collected_bars: list[Bar] = []
        cur = start
        one_d = timedelta(days=1)
        sym_clean = [s.split(":")[-1].strip().upper() for s in symbols_or_isins]
        is_isin = [s for s in symbols_or_isins if s.startswith("IN") and len(s) == 12]

        while cur <= end:
            content = self._fetch_file_content(cur)
            self._handle_trading_signals(cur, content)
            if content:
                rows = parse_bhavcopy_csv(content, target_symbols=sym_clean, target_isins=is_isin)
                day_bars = [bhavcopy_row_to_bar(r, _EXCH_TZ) for r in rows]
                for b in day_bars:
                    if self._store is not None:
                        key = SeriesKey(
                            instrument=b.instrument,
                            kind="bars",
                            timeframe="1d",
                            adjustment="raw",
                            source="nse_bhavcopy",
                        )
                        self._store.commit(key, [b])
                    collected_bars.append(b)
            cur += one_d
        return collected_bars

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Sequence[Bar]:
        """DataSource.history implementation."""
        st_d = start.astimezone(_EXCH_TZ).date()
        en_d = end.astimezone(_EXCH_TZ).date()
        sym = str(key.instrument)
        bars = self.daily_bars([sym], st_d, en_d)
        return [b for b in bars if start <= b.start_ts <= end]

    def instruments(self) -> Sequence[Instrument]:
        """DataSource.instruments implementation."""
        if self._manifest is not None and hasattr(self._manifest, "list_instruments"):
            recs = self._manifest.list_instruments()
            return [
                Instrument(
                    id=InstrumentId(r.id),
                    exchange=r.exchange,
                    segment=r.segment,
                    symbol=r.trading_symbol,
                    kind=r.kind,
                    lot_size=1,
                    tick_size=Decimal("0.05"),
                )
                for r in recs
            ]
        return []

    def stream(self, instruments: Sequence[InstrumentId]) -> AsyncIterator[MarketEvent]:
        raise NotSupported(
            what="nse_bhavcopy reference plugin does not support streaming quotes",
            where="NSEBhavcopyReferencePlugin.stream",
            fix="Use live broker plugin (e.g. upstox) for streaming data",
        )

    def corporate_actions(
        self,
        symbol_or_isin: str,
        start: date | None = None,
        end: date | None = None,
    ) -> Sequence[ActionRecord]:
        return []
