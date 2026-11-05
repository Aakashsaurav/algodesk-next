"""Partitioned directory layout and path resolution for bars and ticks.

Conforms to M2 design:
  Bars:
    Equity/Index:
      1m: .../timeframe=1m/exchange=E/segment=G/symbol=X/adj=A/year=YYYY/YYYY-MM.parquet
      1d: .../timeframe=1d/exchange=E/segment=G/symbol=X/adj=A/daily.parquet
    Futures:
      1m: .../segment=FUT/symbol=U/expiry=YYYY-MM-DD/adj=A/year=YYYY/YYYY-MM.parquet
      1d: .../segment=FUT/symbol=U/expiry=YYYY-MM-DD/adj=A/daily.parquet
    Options:
      1m: .../segment=OPT/symbol=U/expiry=YYYY-MM-DD/strike=K/type=T/adj=A/year=YYYY/YYYY-MM.parquet
      1d: .../segment=OPT/symbol=U/expiry=YYYY-MM-DD/strike=K/type=T/adj=A/daily.parquet

  Ticks:
    Equity/Index: .../ticks/exchange=E/segment=G/symbol=X/date=D/day.parquet
    Futures: .../ticks/exchange=E/segment=FUT/symbol=U/expiry=YYYY-MM-DD/date=D/day.parquet
    Options:
      .../ticks/exchange=E/segment=OPT/symbol=U/expiry=YYYY-MM-DD/strike=K/type=T/date=D/day.parquet
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from algodesk.api.errors import DataError
from algodesk.api.market import (
    IdParts,
    InstrumentId,
    SeriesKey,
    format_id,
    parse_id,
)
from algodesk.data.aliases import resolve_alias

_BASE_TIMEFRAMES = ("1m", "1d")


def sanitize_path_segment(value: str) -> str:
    """Sanitize path component to ensure no spaces or illegal special characters."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())


def instrument_partition_path(instrument: InstrumentId | str) -> Path:
    """Compute relative partitioned directory hierarchy for an instrument.

    Conforms to:
      - Equity/Index: exchange=E/segment=G/symbol=X
      - Futures: exchange=E/segment=FUT/symbol=UNDERLYING/expiry=YYYY-MM-DD
      - Options: exchange=E/segment=OPT/symbol=UNDERLYING/expiry=YYYY-MM-DD/strike=K/type=T
    """
    canonical_id = resolve_alias(instrument)
    parsed = parse_id(canonical_id)

    exch = sanitize_path_segment(parsed.exchange)
    seg = sanitize_path_segment(parsed.segment)
    sym = sanitize_path_segment(parsed.symbol)

    if parsed.segment == "FUT" or (parsed.expiry is not None and parsed.strike is None):
        assert parsed.expiry is not None
        exp_str = sanitize_path_segment(parsed.expiry.isoformat())
        return Path(f"exchange={exch}") / f"segment={seg}" / f"symbol={sym}" / f"expiry={exp_str}"

    if parsed.segment == "OPT" or (parsed.expiry is not None and parsed.strike is not None):
        assert parsed.expiry is not None
        assert parsed.strike is not None
        exp_str = sanitize_path_segment(parsed.expiry.isoformat())
        if parsed.strike == parsed.strike.to_integral_value():
            k_str = str(int(parsed.strike))
        else:
            k_str = format(parsed.strike.normalize(), "f")
        k_sanitized = sanitize_path_segment(k_str)
        t_sanitized = sanitize_path_segment(str(parsed.option_type))
        return (
            Path(f"exchange={exch}")
            / f"segment={seg}"
            / f"symbol={sym}"
            / f"expiry={exp_str}"
            / f"strike={k_sanitized}"
            / f"type={t_sanitized}"
        )

    return Path(f"exchange={exch}") / f"segment={seg}" / f"symbol={sym}"


def dataset_id_for_key(key: SeriesKey) -> str:
    """Format canonical dataset ID for a SeriesKey."""
    canonical_inst = resolve_alias(key.instrument)
    return f"bars:{key.source}:{key.timeframe}:{canonical_inst}:{key.adjustment}"


def bar_file_path(root_dir: Path, key: SeriesKey, dt: date | datetime) -> Path:
    """Resolve destination parquet file path for given SeriesKey and date/time."""
    if key.timeframe not in _BASE_TIMEFRAMES:
        tf_list = ", ".join(_BASE_TIMEFRAMES)
        raise DataError(
            what=(
                f"Unsupported bar timeframe '{key.timeframe}'. "
                f"Stored bar timeframes are limited to base timeframes ({tf_list})"
            ),
            where="layout.bar_file_path",
            fix=f"Request base timeframe ({tf_list}) or use resample() for derived intervals",
        )

    part = instrument_partition_path(key.instrument)
    base = (
        root_dir
        / "bars"
        / f"source={key.source}"
        / f"timeframe={key.timeframe}"
        / part
        / f"adj={key.adjustment}"
    )

    if key.timeframe == "1m":
        year = dt.year
        month = dt.month
        return base / f"year={year}" / f"{year:04d}-{month:02d}.parquet"

    # 1d is stored in a single daily.parquet per instrument/adj/source
    return base / "daily.parquet"


def tick_file_path(
    root_dir: Path,
    instrument: InstrumentId | str,
    dt: date | str,
    filename: str = "day.parquet",
) -> Path:
    """Resolve destination parquet file path for given instrument and date."""
    part = instrument_partition_path(instrument)
    d_str = dt.isoformat() if isinstance(dt, date) else str(dt)
    return root_dir / "ticks" / part / f"date={d_str}" / filename


def parse_bar_file_path(path: Path | str) -> dict[str, Any] | None:
    """Parse a bar file path back to its partition components, or None if not matching."""
    p = Path(path)
    parts = p.parts
    if "bars" not in parts:
        return None

    part_dict: dict[str, str] = {}
    for elem in parts:
        if "=" in elem:
            k, v = elem.split("=", 1)
            part_dict[k] = v

    if not all(
        k in part_dict for k in ("source", "timeframe", "exchange", "segment", "symbol", "adj")
    ):
        return None

    filename = p.name
    year_val = int(part_dict["year"]) if "year" in part_dict else None
    month_val = None
    if filename.endswith(".parquet") and "-" in filename:
        stem = filename[:-8]
        subparts = stem.split("-")
        if len(subparts) == 2 and subparts[1].isdigit():
            month_val = int(subparts[1])

    # Reconstruct canonical InstrumentId
    exp_val: date | None = (
        date.fromisoformat(part_dict["expiry"]) if "expiry" in part_dict else None
    )
    strike_val: Decimal | None = Decimal(part_dict["strike"]) if "strike" in part_dict else None
    type_val: str | None = part_dict.get("type")

    try:
        id_parts = IdParts(
            exchange=part_dict["exchange"],
            segment=part_dict["segment"],
            symbol=part_dict["symbol"],
            expiry=exp_val,
            strike=strike_val,
            option_type=type_val,
        )
        inst_id = format_id(id_parts)
    except Exception:
        inst_id = InstrumentId(
            f"{part_dict['exchange']}:{part_dict['segment']}:{part_dict['symbol']}"
        )

    return {
        "source": part_dict["source"],
        "timeframe": part_dict["timeframe"],
        "exchange": part_dict["exchange"],
        "segment": part_dict["segment"],
        "symbol": part_dict["symbol"],
        "adj": part_dict["adj"],
        "year": year_val,
        "month": month_val,
        "filename": filename,
        "expiry": exp_val,
        "strike": strike_val,
        "type": type_val,
        "instrument": inst_id,
    }


def parse_tick_file_path(path: Path | str) -> dict[str, Any] | None:
    """Parse a tick file path back to its partition components, or None if not matching."""
    p = Path(path)
    parts = p.parts
    if "ticks" not in parts:
        return None

    part_dict: dict[str, str] = {}
    for elem in parts:
        if "=" in elem:
            k, v = elem.split("=", 1)
            part_dict[k] = v

    if not all(k in part_dict for k in ("exchange", "segment", "symbol", "date")):
        return None

    filename = p.name
    exp_val: date | None = (
        date.fromisoformat(part_dict["expiry"]) if "expiry" in part_dict else None
    )
    strike_val: Decimal | None = Decimal(part_dict["strike"]) if "strike" in part_dict else None
    type_val: str | None = part_dict.get("type")

    try:
        id_parts = IdParts(
            exchange=part_dict["exchange"],
            segment=part_dict["segment"],
            symbol=part_dict["symbol"],
            expiry=exp_val,
            strike=strike_val,
            option_type=type_val,
        )
        inst_id = format_id(id_parts)
    except Exception:
        # Fallback to legacy
        raw_id = f"{part_dict['exchange']}:{part_dict['segment']}:{part_dict['symbol']}"
        inst_id = resolve_alias(raw_id)

    return {
        "exchange": part_dict["exchange"],
        "segment": part_dict["segment"],
        "symbol": part_dict["symbol"],
        "date": part_dict["date"],
        "filename": filename,
        "expiry": exp_val,
        "strike": strike_val,
        "type": type_val,
        "instrument": inst_id,
    }
