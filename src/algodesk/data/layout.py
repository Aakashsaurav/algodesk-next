"""Partitioned directory layout and path resolution for historical market bars.

Conforms to M2 design:
  1m = data/bars/source=S/timeframe=1m/exchange=E/segment=G/symbol=X/adj=A/year=YYYY/YYYY-MM.parquet
  1d = data/bars/source=S/timeframe=1d/exchange=E/segment=G/symbol=X/adj=A/daily.parquet
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from algodesk.api.errors import DataError
from algodesk.api.market import SeriesKey, parse_id

_BASE_TIMEFRAMES = ("1m", "1d")

_BAR_PATH_RE = re.compile(
    r".*bars/source=(?P<source>[^/]+)/timeframe=(?P<timeframe>[^/]+)/exchange=(?P<exchange>[^/]+)/segment=(?P<segment>[^/]+)/symbol=(?P<symbol>[^/]+)/adj=(?P<adj>[^/]+)(?:/year=(?P<year>\d{4}))?/(?P<filename>[^/]+)$"
)


def dataset_id_for_key(key: SeriesKey) -> str:
    """Format canonical dataset ID for a SeriesKey."""
    return f"bars:{key.source}:{key.timeframe}:{key.instrument}:{key.adjustment}"


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

    parsed = parse_id(key.instrument)
    base = (
        root_dir
        / "bars"
        / f"source={key.source}"
        / f"timeframe={key.timeframe}"
        / f"exchange={parsed.exchange}"
        / f"segment={parsed.segment}"
        / f"symbol={parsed.symbol}"
        / f"adj={key.adjustment}"
    )

    if key.timeframe == "1m":
        year = dt.year
        month = dt.month
        return base / f"year={year}" / f"{year:04d}-{month:02d}.parquet"

    # 1d is stored in a single daily.parquet per instrument/adj/source
    return base / "daily.parquet"


def parse_bar_file_path(path: Path | str) -> dict[str, Any] | None:
    """Parse a bar file path back to its partition components, or None if not matching."""
    s = str(path).replace("\\", "/")
    m = _BAR_PATH_RE.search(s)
    if not m:
        return None
    res = m.groupdict()
    year_val = int(res["year"]) if res.get("year") else None
    month_val = None
    filename = res["filename"]
    if filename.endswith(".parquet") and "-" in filename:
        stem = filename[:-8]
        parts = stem.split("-")
        if len(parts) == 2 and parts[1].isdigit():
            month_val = int(parts[1])

    return {
        "source": res["source"],
        "timeframe": res["timeframe"],
        "exchange": res["exchange"],
        "segment": res["segment"],
        "symbol": res["symbol"],
        "adj": res["adj"],
        "year": year_val,
        "month": month_val,
        "filename": filename,
    }
