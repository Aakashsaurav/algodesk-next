"""Intraday live session bar storage isolated from historical backtest bars.

Stored at data/session_bars/date=YYYY-MM-DD/, separate from history and never merged.
Purged automatically according to data.session_bars_keep_days.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from algodesk.api.market import Bar, InstrumentId, parse_id
from algodesk.api.services import ParamReader
from algodesk.data.schema import read_bars_parquet, write_bars_parquet


class SessionBarStore:
    """Manages ephemeral intraday session bars separated from long-term history."""

    __slots__ = ("_base_dir", "_keep_days")

    def __init__(
        self,
        data_dir: Path | str,
        params: ParamReader | None = None,
        keep_days: int | None = None,
    ) -> None:
        self._base_dir = Path(data_dir) / "session_bars"
        if keep_days is not None:
            self._keep_days = keep_days
        elif params is not None:
            try:
                self._keep_days = int(params.get("data.session_bars_keep_days"))
            except Exception:
                self._keep_days = 1
        else:
            self._keep_days = 1

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def keep_days(self) -> int:
        return self._keep_days

    def path_for(self, day: date, instrument: InstrumentId) -> Path:
        """Resolve file path for a symbol on a specific session date."""
        parsed = parse_id(instrument)
        return self._base_dir / f"date={day.isoformat()}" / f"{parsed.symbol}.parquet"

    def record_bars(
        self,
        day: date,
        instrument: InstrumentId,
        bars: Sequence[Bar],
    ) -> Path:
        """Persist completed live session bars to isolated date partition."""
        p = self.path_for(day, instrument)
        p.parent.mkdir(parents=True, exist_ok=True)
        write_bars_parquet(p, bars)
        return p

    def load_bars(
        self,
        day: date,
        instrument: InstrumentId,
        timeframe: str = "1m",
    ) -> list[Bar]:
        """Read session bars for given day and instrument."""
        p = self.path_for(day, instrument)
        if not p.exists():
            return []
        return read_bars_parquet(p, instrument, timeframe)

    def purge_older_than(self, days: int | None = None) -> int:
        """Purge session bar date folders older than keep_days retention."""
        limit_days = days if days is not None else self._keep_days
        if not self._base_dir.exists():
            return 0

        cutoff = (datetime.now(UTC) - timedelta(days=limit_days)).date()
        purged = 0

        for d_dir in self._base_dir.iterdir():
            if not d_dir.is_dir() or not d_dir.name.startswith("date="):
                continue
            date_str = d_dir.name[5:]
            try:
                d_val = date.fromisoformat(date_str)
            except ValueError:
                continue

            if d_val < cutoff:
                shutil.rmtree(d_dir, ignore_errors=True)
                purged += 1

        return purged
