"""Bhavcopy parsing for legacy and unified UDiFF formats."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, tzinfo
from decimal import Decimal

from algodesk.api.market import Bar, InstrumentId


@dataclass(frozen=True, slots=True)
class BhavcopyRow:
    """Parsed single equity row from official NSE Bhavcopy."""

    symbol: str
    series: str
    isin: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    prev_close: Decimal
    volume: int


def _parse_legacy_date(date_str: str) -> date:
    """Parse legacy date string like '15-MAY-2024' or '28-OCT-2024'."""
    clean = date_str.strip().upper()
    try:
        return datetime.strptime(clean, "%d-%b-%Y").date()
    except ValueError:
        return date.fromisoformat(clean)


def _parse_decimal(val: str) -> Decimal:
    clean = val.strip().replace(",", "")
    return Decimal(clean) if clean else Decimal(0)


def _parse_int(val: str) -> int:
    clean = val.strip().replace(",", "")
    return int(float(clean)) if clean else 0


def _parse_udiff_row(
    row: dict[str, str],
    target_symbols: set[str] | None,
    target_isins: set[str] | None,
) -> BhavcopyRow | None:
    sym = row.get("TckrSymb", "").strip().upper()
    srs = row.get("SctySrs", "").strip().upper()
    isin = row.get("ISIN", "").strip().upper()
    if srs not in ("EQ", "BE"):
        return None
    if target_symbols and sym not in target_symbols and isin not in (target_isins or ()):
        return None
    if target_isins and isin not in target_isins and sym not in (target_symbols or ()):
        return None

    dt_str = row.get("TradDt", "").strip()
    tr_date = date.fromisoformat(dt_str) if dt_str else date.today()
    return BhavcopyRow(
        symbol=sym,
        series=srs,
        isin=isin,
        trade_date=tr_date,
        open=_parse_decimal(row.get("OpnPric", "0")),
        high=_parse_decimal(row.get("HghPric", "0")),
        low=_parse_decimal(row.get("LwPric", "0")),
        close=_parse_decimal(row.get("ClsPric", "0")),
        prev_close=_parse_decimal(row.get("PrvsClsgPric", "0")),
        volume=_parse_int(row.get("TtlTradgVol", "0")),
    )


def _parse_legacy_row(
    row: dict[str, str],
    target_symbols: set[str] | None,
    target_isins: set[str] | None,
) -> BhavcopyRow | None:
    sym = row.get("SYMBOL", "").strip().upper()
    srs = row.get("SERIES", "").strip().upper()
    isin = row.get("ISIN", "").strip().upper()
    if srs not in ("EQ", "BE"):
        return None
    if target_symbols and sym not in target_symbols and isin not in (target_isins or ()):
        return None
    if target_isins and isin not in target_isins and sym not in (target_symbols or ()):
        return None

    dt_str = row.get("TIMESTAMP", "").strip()
    tr_date = _parse_legacy_date(dt_str) if dt_str else date.today()
    return BhavcopyRow(
        symbol=sym,
        series=srs,
        isin=isin,
        trade_date=tr_date,
        open=_parse_decimal(row.get("OPEN", "0")),
        high=_parse_decimal(row.get("HIGH", "0")),
        low=_parse_decimal(row.get("LOW", "0")),
        close=_parse_decimal(row.get("CLOSE", "0")),
        prev_close=_parse_decimal(row.get("PREVCLOSE", "0")),
        volume=_parse_int(row.get("TOTTRDQTY", "0")),
    )


def parse_bhavcopy_csv(
    csv_text: str,
    target_symbols: Sequence[str] | None = None,
    target_isins: Sequence[str] | None = None,
) -> list[BhavcopyRow]:
    """Parse CSV text into list of BhavcopyRow for specified universe symbols/ISINs."""
    if not csv_text.strip():
        return []

    sym_set = {s.split(":")[-1].strip().upper() for s in target_symbols} if target_symbols else None
    isin_set = {i.strip().upper() for i in target_isins} if target_isins else None

    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = set(reader.fieldnames or [])
    is_udiff = "TckrSymb" in fieldnames

    parsed: list[BhavcopyRow] = []
    for raw_row in reader:
        rec = (
            _parse_udiff_row(raw_row, sym_set, isin_set)
            if is_udiff
            else _parse_legacy_row(raw_row, sym_set, isin_set)
        )
        if rec is not None:
            parsed.append(rec)
    return parsed


def bhavcopy_row_to_bar(row: BhavcopyRow, exchange_tz: tzinfo) -> Bar:
    """Convert BhavcopyRow to 1d canonical Bar with raw prices."""
    d = row.trade_date
    start_ts = datetime.combine(d, time(9, 15), tzinfo=exchange_tz).astimezone(UTC)
    end_ts = datetime.combine(d, time(15, 30), tzinfo=exchange_tz).astimezone(UTC)
    return Bar(
        instrument=InstrumentId(f"NSE:EQ:{row.symbol}"),
        timeframe="1d",
        start_ts=start_ts,
        end_ts=end_ts,
        open=float(row.open),
        high=float(row.high),
        low=float(row.low),
        close=float(row.close),
        volume=row.volume,
        complete=True,
        synthetic=False,
    )
