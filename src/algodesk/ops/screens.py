"""Ops injection and adapters for screen execution collaborators."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from algodesk.api.errors import NotSupported
from algodesk.api.kinds import Mode, PluginKind
from algodesk.api.market import InstrumentId, SeriesKey
from algodesk.api.protocols import DataSource
from algodesk.api.screens import (
    IndicatorResolver,
    ScreenBars,
    ScreenDataReader,
    ScreenRefusal,
    SkipReason,
)
from algodesk.data.calendar_cache import CachedCalendar
from algodesk.data.manifest import DataManifest
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.ops.context import build_context
from algodesk.ops.runtime import Runtime
from algodesk.screens.runner import ScreenRunner

logger = logging.getLogger("algodesk.ops.screens")


class DataServiceScreenAdapter(ScreenDataReader):
    """Adapts DataService to the foundation ScreenDataReader protocol."""

    def __init__(
        self,
        data_service: DataService,
        default_source: str | None = None,
    ) -> None:
        self._ds = data_service
        self._default_source: str = str(
            default_source or getattr(data_service, "_default_source", "upstox") or "upstox"
        )

    # Map logical adjust basis to SeriesKey.adjustment values (only "raw"/"adjusted" allowed).
    # "splits" data is stored as "adjusted" (source-adjusted) in the BarStore.
    # "none" data is stored as "raw" (unadjusted) in the BarStore.
    _ADJUST_TO_DOWNLOAD: dict[str, str] = {
        "total": "adjusted",
        "splits": "adjusted",
        "none": "raw",
        "raw": "raw",
        "adjusted": "adjusted",
    }

    def get_bars(
        self,
        instrument: InstrumentId | str,
        timeframe: str,
        start: datetime,
        end: datetime,
        adjust: str = "splits",
        accept_unverified: bool = False,
    ) -> ScreenBars | ScreenRefusal:
        inst_str = str(instrument)
        inst_id = InstrumentId(inst_str if ":" in inst_str else f"NSE:EQ:{inst_str}")
        # Map logical basis to a valid SeriesKey adjustment value
        download = self._ADJUST_TO_DOWNLOAD.get(adjust, "adjusted")
        key = SeriesKey(
            instrument=inst_id,
            kind="bars",
            timeframe=timeframe,
            adjustment=download,
            source=self._default_source,
        )

        try:
            slice_res = self._ds.get(
                series=key,
                start=start,
                end=end,
                download=download,
                adjust=adjust if adjust in ("none", "splits", "total") else "splits",
                fill_only=False,
                source=self._default_source,
                accept_unverified=accept_unverified,
                columns=("start_ts", "end_ts", "open", "high", "low", "close", "volume"),
            )
        except NotSupported as exc:
            return ScreenRefusal(reason=SkipReason.ADJUSTMENT_REFUSED, detail=str(exc))
        except Exception as exc:
            msg = str(exc).lower()
            if "gap" in msg or "calendar" in msg:
                return ScreenRefusal(reason=SkipReason.CALENDAR_GAP, detail=str(exc))
            if (
                "adjust" in msg
                or "conflict" in msg
                or "corporate action" in msg
                or "manual_required" in msg
                or "action" in msg
                and "status" in msg
            ):
                sym_str = str(instrument).split(":")[-1]
                detail = (
                    f"Adjustment refused for {sym_str}: {exc}. "
                    f"Fix with: algodesk data actions override add --symbol {sym_str}"
                )
                return ScreenRefusal(reason=SkipReason.ADJUSTMENT_REFUSED, detail=detail)
            return ScreenRefusal(reason=SkipReason.NO_DATA, detail=str(exc))

        if not slice_res.bars:
            return ScreenRefusal(
                reason=SkipReason.NO_DATA,
                detail=f"No bars found for {instrument} between {start.date()} and {end.date()}",
            )

        return ScreenBars(
            bars=slice_res.bars,
            provenance=slice_res.provenance_info,
        )


class PluginIndicatorResolver(IndicatorResolver):
    """Resolves indicators using the plugin registry or builtin indicators plugin."""

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry
        self._cached_plugin: Any = None

    def get_indicator(self, name: str, **params: Any) -> Any:
        low_name = name.lower()
        if self._registry is not None:
            try:
                # Check if indicators builtin plugin is available
                if self._cached_plugin is None:
                    p_ctx = build_context(Mode.RESEARCH, run_id="screen_indicators")
                    self._cached_plugin = self._registry.create(
                        PluginKind.INDICATOR, "builtin", config={}, ctx=p_ctx
                    )
                if hasattr(self._cached_plugin, "get_indicator"):
                    return self._cached_plugin.get_indicator(low_name, **params)
            except Exception:
                pass

        # Fallback to dynamic loading if inside ops
        try:
            import importlib

            mod = importlib.import_module("plugins.indicators.builtin.plugin")
            plugin_cls = mod.BuiltinIndicatorsPlugin
            return plugin_cls.get_indicator(low_name, **params)
        except Exception as exc:
            raise ValueError(f"Could not resolve indicator '{name}': {exc}") from exc


def build_data_service(runtime: Runtime, source: str = "upstox") -> DataService:
    """Construct a configured DataService from active runtime."""
    p_ctx = build_context(Mode.RESEARCH, run_id=f"screen_ds_{source}")
    data_dir = getattr(getattr(runtime, "paths", None), "data", Path("data"))
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_db = state_dir / "data_manifest.db"
    manifest = DataManifest(manifest_db)

    store = BarStore(
        data_dir,
        manifest,
        params=runtime.params,
        journal=getattr(runtime, "journal", None),
    )

    sources_dict: dict[str, DataSource] = {}
    try:
        broker = cast(
            DataSource,
            runtime.plugins.create(PluginKind.BROKER, source, config={}, ctx=p_ctx),
        )
        sources_dict[source] = broker
    except Exception:
        pass

    cal: Any = None
    try:
        cal = runtime.plugins.create(
            PluginKind.CALENDAR, "exchange", config={"exchange": "nse"}, ctx=p_ctx
        )
    except Exception:
        pass

    manifests = {
        p.manifest.name: p.manifest for p in runtime.plugins.list() if hasattr(p, "manifest")
    }

    return DataService(
        sources_dict,
        source,
        store,
        None,
        manifests=manifests,
        calendar=cal,
        params=runtime.params,
        journal=getattr(runtime, "journal", None),
    )


def build_screen_runner(
    runtime: Runtime,
    data_service: DataService | None = None,
    calendar: Any = None,
    max_skipped_pct: float | None = None,
) -> ScreenRunner:
    """Build a fully wired ScreenRunner injecting adapted protocols."""
    ds = data_service or build_data_service(runtime)
    data_adapter = DataServiceScreenAdapter(ds)
    indicator_resolver = PluginIndicatorResolver(runtime.plugins)

    cal = calendar
    if cal is None:
        try:
            p_ctx = build_context(Mode.RESEARCH, run_id="screen_cal")
            cal = runtime.plugins.create(
                PluginKind.CALENDAR, "exchange", config={"exchange": "nse"}, ctx=p_ctx
            )
        except Exception:
            cal = getattr(ds, "_calendar", None)
    cal = CachedCalendar.wrap(cal)

    # Read screen.max_skipped_pct from parameter registry
    skip_pct = 10.0
    progress_every = 25
    if max_skipped_pct is not None:
        skip_pct = float(max_skipped_pct)
    elif runtime.params is not None:
        try:
            val = runtime.params.get("screen.max_skipped_pct")
            if val is not None:
                skip_pct = float(val)
        except Exception:
            pass
        try:
            p_val = runtime.params.get("cli.progress_every")
            if p_val is not None:
                progress_every = int(p_val)
        except Exception:
            pass

    return ScreenRunner(
        data_reader=data_adapter,
        indicator_resolver=indicator_resolver,
        calendar=cal,
        max_skipped_pct=skip_pct,
        progress_every=progress_every,
    )
