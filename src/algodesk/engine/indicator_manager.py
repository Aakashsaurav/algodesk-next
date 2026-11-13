"""Incremental indicator management and warm-up for technical indicators.

Maintains one incremental indicator state per (indicator, instrument, timeframe, params),
handles historical warm-up from DataService without look-ahead, and feeds only completed bars.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    current_values: dict[str, float | None] = field(default_factory=dict)
    bars_fed: int = 0
    is_warmed: bool = False
    values_history: list[float] = field(default_factory=list)


def _feed_bar_to_entry(entry: _IndicatorEntry, bar: Bar) -> None:
    inputs = entry.indicator.spec.inputs if hasattr(entry.indicator, "spec") else ("close",)
    if len(inputs) == 1:
        f = inputs[0]
        if f == "close":
            val_in: Any = bar.close
        elif f == "volume":
            val_in = float(bar.volume)
        else:
            val_in = float(getattr(bar, f, bar.close))
    else:
        val_in = bar

    res = entry.indicator.update(entry.state, val_in)
    spec_outputs = entry.indicator.spec.outputs if hasattr(entry.indicator, "spec") else ("value",)
    first_output = spec_outputs[0] if spec_outputs else "value"

    if isinstance(res, Mapping):
        entry.current_values = {str(k): v for k, v in res.items()}
        entry.current_value = entry.current_values.get(first_output)
    else:
        entry.current_value = res
        entry.current_values = {first_output: res}

    entry.bars_fed += 1
    if entry.current_value is not None:
        entry.values_history.append(entry.current_value)
    if entry.bars_fed >= entry.warmup_needed and entry.current_value is not None:
        entry.is_warmed = True


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
        self._warmed_bars: list[Bar] = []

    @property
    def warmup_bars(self) -> list[Bar]:
        """Bars used to warm up indicators prior to session start."""
        return list(self._warmed_bars)

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
        clean_params = dict(params)
        clean_params.pop("output", None)
        key = self._make_key(name, instrument, timeframe, clean_params)
        if key in self._indicators:
            return self._indicators[key].indicator

        if self._plugin is None:
            raise ValueError("IndicatorManager has no indicator plugin configured")

        if hasattr(self._plugin, "get_indicator"):
            ind = cast(Indicator, self._plugin.get_indicator(name, **clean_params))
        else:
            ind = cast(Indicator, self._plugin)

        spec = getattr(ind, "spec", None)
        if spec is not None and getattr(spec, "timeframes", None):
            tf_low = timeframe.lower()
            if "intraday" in spec.timeframes:
                tf_s = parse_timeframe_seconds(timeframe)
                if tf_s >= 86400 or tf_low.endswith("d") or tf_low.endswith("w"):
                    raise ValueError(
                        f"Indicator '{name}' only supports intraday timeframes, got '{timeframe}'"
                    )
            elif timeframe not in spec.timeframes:
                raise ValueError(
                    f"Indicator '{name}' only supports timeframes {spec.timeframes}, "
                    f"got '{timeframe}'"
                )

        warmup = getattr(spec, "warmup", 0) if spec else 0
        default_p = 14 if warmup == 0 else warmup + 1
        period = int(clean_params.get("n", clean_params.get("period", default_p)))
        warmup_needed = max(warmup + 1, period)

        entry = _IndicatorEntry(
            name=name.lower(),
            instrument=InstrumentId(str(instrument)),
            timeframe=timeframe,
            params=clean_params,
            indicator=ind,
            state=ind.new_state(),
            warmup_needed=warmup_needed,
            current_value=None,
            current_values={},
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
            if b not in self._warmed_bars:
                self._warmed_bars.append(b)
            for e in entries:
                _feed_bar_to_entry(e, b)

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

    def warmup_from_bars(self, bars: Sequence[Bar]) -> None:
        """Warm up registered indicators directly from an explicit sequence of Bar objects."""
        if not self._indicators or not bars:
            return
        self._warmed_bars = list(bars)
        series_groups: dict[tuple[InstrumentId, str], list[_IndicatorEntry]] = {}
        for entry in self._indicators.values():
            grp_key = (entry.instrument, entry.timeframe)
            series_groups.setdefault(grp_key, []).append(entry)

        sorted_bars = sorted(bars, key=lambda b: b.start_ts)
        for b in sorted_bars:
            grp = series_groups.get((b.instrument, b.timeframe))
            if grp:
                for e in grp:
                    _feed_bar_to_entry(e, b)

        for entry in self._indicators.values():
            entry.is_warmed = (
                entry.bars_fed >= entry.warmup_needed and entry.current_value is not None
            )

    def on_bar(self, bar: Bar) -> None:
        """Feed a bar incrementally. Only completed bars are fed (no look-ahead)."""
        if not getattr(bar, "complete", True):
            return

        for entry in self._indicators.values():
            if entry.instrument == bar.instrument and entry.timeframe == bar.timeframe:
                _feed_bar_to_entry(entry, bar)

    def get(
        self,
        name: str,
        instrument: InstrumentId | str,
        timeframe: str,
        output: str | None = None,
        **params: Any,
    ) -> float | None:
        """Get the current indicator value. Returns None if not yet warmed up or ready."""
        clean_params = dict(params)
        out = output or clean_params.pop("output", None)
        key = self._make_key(name, instrument, timeframe, clean_params)
        entry = self._indicators.get(key)
        if entry is None:
            try:
                self.register(name, instrument, timeframe, **clean_params)
                entry = self._indicators.get(key)
            except Exception:
                return None

        if entry is None or not entry.is_warmed:
            return None

        spec_outputs = (
            entry.indicator.spec.outputs if hasattr(entry.indicator, "spec") else ("value",)
        )
        target = out or (spec_outputs[0] if spec_outputs else "value")
        return entry.current_values.get(target)

    def is_ready(
        self,
        name: str,
        instrument: InstrumentId | str,
        timeframe: str,
        **params: Any,
    ) -> bool:
        """Check whether the indicator has completed warm-up."""
        clean_params = dict(params)
        clean_params.pop("output", None)
        key = self._make_key(name, instrument, timeframe, clean_params)
        entry = self._indicators.get(key)
        return entry.is_warmed if entry is not None else False
