"""Incremental indicator management and warm-up for technical indicators.

Maintains one incremental indicator state per (indicator, instrument, timeframe, params),
handles historical warm-up from DataService without look-ahead, and feeds only completed bars.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

from algodesk.api.market import Bar, InstrumentId, SeriesKey
from algodesk.api.protocols import Indicator
from algodesk.api.services import JournalWriter
from algodesk.engine.bars import parse_timeframe_seconds

IndicatorKey = tuple[str, InstrumentId, str, tuple[tuple[str, Any], ...]]


@dataclass
class _IndicatorEntry:
    name: str
    instrument: InstrumentId
    timeframe: str
    params: dict[str, Any]
    indicator: Indicator
    state: object
    warmup_needed: int
    current_value: float | None = None
    bars_fed: int = 0
    is_warmed: bool = False
    values_history: list[float] = field(default_factory=list)


class IndicatorManager:
    """Manages incremental indicator instances across instruments and timeframes."""

    def __init__(
        self,
        indicator_plugin: Any | None = None,
        journal: JournalWriter | Any | None = None,
    ) -> None:
        self._plugin = indicator_plugin
        self._journal = journal
        self._indicators: dict[IndicatorKey, _IndicatorEntry] = {}

    def _make_key(
        self,
        name: str,
        instrument: InstrumentId | str,
        timeframe: str,
        params: Mapping[str, Any],
    ) -> IndicatorKey:
        inst_id = InstrumentId(str(instrument))
        sorted_params = tuple(sorted(params.items()))
        return (name.lower(), inst_id, timeframe, sorted_params)

    def register(
        self,
        name: str,
        instrument: InstrumentId | str,
        timeframe: str,
        **params: Any,
    ) -> Indicator:
        key = self._make_key(name, instrument, timeframe, params)
        if key in self._indicators:
            return self._indicators[key].indicator

        if self._plugin is None:
            raise ValueError("IndicatorManager has no indicator plugin configured")

        if hasattr(self._plugin, "get_indicator"):
            ind = cast(Indicator, self._plugin.get_indicator(name, **params))
        else:
            ind = cast(Indicator, self._plugin)

        warmup = getattr(getattr(ind, "spec", None), "warmup", 0)
        period = int(params.get("period", 14 if warmup == 0 else warmup + 1))
        warmup_needed = max(warmup + 1, period)

        entry = _IndicatorEntry(
            name=name.lower(),
            instrument=InstrumentId(str(instrument)),
            timeframe=timeframe,
            params=dict(params),
            indicator=ind,
            state=ind.new_state(),
            warmup_needed=warmup_needed,
            current_value=None,
            bars_fed=0,
            is_warmed=False,
        )
        self._indicators[key] = entry
        return ind

    def warmup(
        self,
        data_service: Any,
        session_start: datetime,
        source: str | None = None,
    ) -> None:
        """Warm up registered indicators using historical bars prior to session start."""
        if not self._indicators:
            return

        series_groups: dict[tuple[InstrumentId, str], list[_IndicatorEntry]] = {}
        for entry in self._indicators.values():
            grp_key = (entry.instrument, entry.timeframe)
            series_groups.setdefault(grp_key, []).append(entry)

        for (inst, tf), entries in series_groups.items():
            self._warmup_series(entries, inst, tf, data_service, session_start, source)

    def _warmup_series(
        self,
        entries: list[_IndicatorEntry],
        inst: InstrumentId,
        tf: str,
        data_service: Any,
        session_start: datetime,
        source: str | None,
    ) -> None:
        max_warmup = max(e.warmup_needed for e in entries)
        if max_warmup <= 0:
            for e in entries:
                e.is_warmed = True
            return

        tf_seconds = parse_timeframe_seconds(tf)
        lookback_s = max(86400, max_warmup * tf_seconds * 10)
        start_dt = session_start - timedelta(seconds=lookback_s)

        src = source or (
            data_service.default_source() if hasattr(data_service, "default_source") else "upstox"
        )
        key = SeriesKey(
            instrument=inst,
            kind="bars",
            timeframe=tf,
            adjustment="raw",
            source=src,
        )

        try:
            from algodesk.data.service import DataService

            if isinstance(data_service, DataService):
                slice_res = data_service.get(key, start=start_dt, end=session_start)
                bars = list(slice_res.bars)
            elif hasattr(data_service, "history"):
                bars = list(data_service.history(key, start=start_dt, end=session_start))
            elif hasattr(data_service, "get"):
                slice_res = data_service.get(key, start=start_dt, end=session_start)
                bars = list(getattr(slice_res, "bars", []))
            else:
                bars = []
        except Exception:
            # pyrefly: ignore[implicit-any-empty-container]
            bars = []

        completed_bars = [
            b for b in bars if getattr(b, "complete", True) and b.end_ts <= session_start
        ]
        feed_bars = (
            completed_bars[-max_warmup:] if len(completed_bars) > max_warmup else completed_bars
        )

        for b in feed_bars:
            for e in entries:
                val = e.indicator.update(e.state, b.close)
                e.current_value = val
                e.bars_fed += 1
                if val is not None:
                    e.values_history.append(val)

        for e in entries:
            if e.bars_fed >= e.warmup_needed and e.current_value is not None:
                e.is_warmed = True
            else:
                e.is_warmed = False
                if self._journal is not None and hasattr(self._journal, "append"):
                    self._journal.append(
                        "strategy.warmup_incomplete",
                        {
                            "instrument": str(inst),
                            "timeframe": tf,
                            "indicator": e.name,
                            "warmup_needed": e.warmup_needed,
                            "bars_available": e.bars_fed,
                        },
                    )

    def on_bar(self, bar: Bar) -> None:
        """Feed a bar incrementally. Only completed bars are fed (no look-ahead)."""
        if not getattr(bar, "complete", True):
            return

        for entry in self._indicators.values():
            if entry.instrument == bar.instrument and entry.timeframe == bar.timeframe:
                val = entry.indicator.update(entry.state, bar.close)
                entry.current_value = val
                entry.bars_fed += 1
                if val is not None:
                    entry.values_history.append(val)
                if entry.bars_fed >= entry.warmup_needed:
                    entry.is_warmed = True

    def get(
        self,
        name: str,
        instrument: InstrumentId | str,
        timeframe: str,
        **params: Any,
    ) -> float | None:
        """Get the current indicator value. Returns None if not yet warmed up or ready."""
        key = self._make_key(name, instrument, timeframe, params)
        entry = self._indicators.get(key)
        if entry is None:
            try:
                self.register(name, instrument, timeframe, **params)
                entry = self._indicators.get(key)
            except Exception:
                return None

        if entry is None or not entry.is_warmed:
            return None

        return entry.current_value

    def is_ready(
        self,
        name: str,
        instrument: InstrumentId | str,
        timeframe: str,
        **params: Any,
    ) -> bool:
        """Check whether the indicator has completed warm-up."""
        key = self._make_key(name, instrument, timeframe, params)
        entry = self._indicators.get(key)
        return entry.is_warmed if entry is not None else False
