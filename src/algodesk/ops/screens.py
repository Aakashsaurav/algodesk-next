"""Ops injection and adapters for screen execution collaborators."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from algodesk.api.errors import ConfigError, NotSupported, SafetyViolation
from algodesk.api.kinds import Mode, PluginKind
from algodesk.api.market import InstrumentId, SeriesKey
from algodesk.api.protocols import Calendar, Clock, DataSource, ScreenSelection, ScreenService
from algodesk.api.screens import (
    IndicatorResolver,
    ScreenBars,
    ScreenDataReader,
    ScreenRefusal,
    ScreenResult,
    SkipReason,
)
from algodesk.data.calendar_cache import CachedCalendar
from algodesk.data.coverage import resolve_trading_sessions
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
    # For Indian markets (Upstox, Bhavcopy), candles in "splits" basis are stored in "raw".
    _ADJUST_TO_DOWNLOAD: dict[str, str] = {
        "total": "adjusted",
        "splits": "raw",
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
                cache_only=True,
                columns=("start_ts", "end_ts", "open", "high", "low", "close", "volume"),
            )
        except NotSupported as exc:
            return ScreenRefusal(reason=SkipReason.ADJUSTMENT_REFUSED, detail=str(exc))
        except Exception as exc:
            msg = str(exc).lower()
            sym_str = str(instrument).split(":")[-1]
            if "gap" in msg or "calendar" in msg:
                detail = (
                    f"Calendar gap for {sym_str}: {exc}. "
                    f"Fix with: algodesk data accumulate --group G --timeframe 1d"
                )
                return ScreenRefusal(reason=SkipReason.NO_DATA, detail=detail)
            if (
                "adjust" in msg
                or "conflict" in msg
                or "corporate action" in msg
                or "manual_required" in msg
                or "action" in msg
                and "status" in msg
            ):
                detail = (
                    f"Adjustment refused for {sym_str}: {exc}. "
                    f"Fix with: algodesk data actions override add --symbol {sym_str}"
                )
                return ScreenRefusal(reason=SkipReason.ADJUSTMENT_REFUSED, detail=detail)
            detail = (
                f"Data error for {sym_str}: {exc}. "
                f"Fix with: algodesk data accumulate --group G --timeframe 1d"
            )
            return ScreenRefusal(reason=SkipReason.NO_DATA, detail=detail)

        sym_str = str(instrument).split(":")[-1]
        store = getattr(self._ds, "_store", None)
        manifest = getattr(store, "manifest", None) if store else None
        target_ds_id = (
            store.dataset_id(key)
            if store
            else f"bars:{key.source}:{key.timeframe}:{key.instrument}:{key.adjustment}"
        )
        raw_id = f"bars:{key.source}:{key.timeframe}:{key.instrument}:raw"

        if not slice_res.bars:
            has_cov = False
            if manifest is not None:
                recs = manifest.get_coverage_days(
                    target_ds_id,
                    start_day=start.date().isoformat(),
                    end_day=end.date().isoformat(),
                )
                if not recs and key.adjustment != "raw":
                    recs = manifest.get_coverage_days(
                        raw_id,
                        start_day=start.date().isoformat(),
                        end_day=end.date().isoformat(),
                    )
                if any(str(r.status) == "COMPLETE" for r in recs):
                    has_cov = True

            if has_cov:
                return ScreenRefusal(
                    reason=SkipReason.NO_DATA,
                    detail=(
                        f"No cached bars found for {sym_str} between "
                        f"{start.date()} and {end.date()}. "
                        "coverage and bars disagree: run algodesk data coverage repair"
                    ),
                )
            return ScreenRefusal(
                reason=SkipReason.NO_DATA,
                detail=(
                    f"No cached bars found for {sym_str} between {start.date()} and {end.date()}. "
                    f"Fix with: algodesk data accumulate --group G --timeframe 1d"
                ),
            )

        # Check for gaps within the needed window
        exch_tz = getattr(self._ds, "_exchange_tz", ZoneInfo("Asia/Kolkata"))
        bar_days = {
            (
                b.start_ts.astimezone(exch_tz).date()
                if b.start_ts.tzinfo
                else b.start_ts.replace(tzinfo=UTC).astimezone(exch_tz).date()
            )
            for b in slice_res.bars
        }
        earliest_bar_date = slice_res.bars[0].start_ts.date()
        end_date = end.date()
        start_date = start.date()
        as_of_date = end.astimezone(exch_tz).date()
        window_gaps = [
            g for g in slice_res.gaps if max(start_date, earliest_bar_date) <= g.day <= end_date
        ]

        store = getattr(self._ds, "_store", None)
        manifest = getattr(store, "manifest", None) if store else None

        # If coverage says covered for a day (e.g. as_of or window gap) but no bar exists:
        if manifest is not None:
            candidates = list(dict.fromkeys([as_of_date] + [g.day for g in window_gaps]))
            for check_d in candidates:
                if check_d < earliest_bar_date or check_d > end_date:
                    continue
                if check_d not in bar_days:
                    day_str = check_d.isoformat()
                    recs = manifest.get_coverage_days(
                        target_ds_id, start_day=day_str, end_day=day_str
                    )
                    if not recs and key.adjustment != "raw":
                        recs = manifest.get_coverage_days(
                            raw_id, start_day=day_str, end_day=day_str
                        )
                    if any(str(r.status) == "COMPLETE" for r in recs):
                        return ScreenRefusal(
                            reason=SkipReason.GAP_IN_WINDOW,
                            detail=(
                                f"Gap in window on {day_str}. "
                                "coverage and bars disagree: run algodesk data coverage repair"
                            ),
                        )

        if window_gaps:
            first_gap = min(g.day for g in window_gaps)
            has_cov = False
            if manifest is not None:
                day_str = first_gap.isoformat()
                recs = manifest.get_coverage_days(target_ds_id, start_day=day_str, end_day=day_str)
                if not recs and key.adjustment != "raw":
                    recs = manifest.get_coverage_days(raw_id, start_day=day_str, end_day=day_str)
                if any(str(r.status) == "COMPLETE" for r in recs):
                    has_cov = True

            if has_cov:
                return ScreenRefusal(
                    reason=SkipReason.GAP_IN_WINDOW,
                    detail=(
                        f"Gap in window on {first_gap.isoformat()}. "
                        "coverage and bars disagree: run algodesk data coverage repair"
                    ),
                )
            return ScreenRefusal(
                reason=SkipReason.GAP_IN_WINDOW,
                detail=(
                    f"Gap in window on {first_gap.isoformat()}. "
                    f"Fix with: algodesk data accumulate --group G --timeframe 1d"
                ),
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


def build_data_service(
    runtime: Runtime,
    source: str = "upstox",
    calendar: Any = None,
) -> DataService:
    """Construct a configured DataService from active runtime."""
    p_ctx = build_context(Mode.RESEARCH, run_id=f"screen_ds_{source}")
    data_dir = getattr(getattr(runtime, "paths", None), "data", Path("data"))
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_db = state_dir / "data_manifest.db"
    manifest = DataManifest(manifest_db)

    cal: Any = calendar
    if cal is None:
        try:
            cal = runtime.plugins.create(
                PluginKind.CALENDAR, "exchange", config={"exchange": "nse"}, ctx=p_ctx
            )
        except Exception:
            pass

    store = BarStore(
        data_dir,
        manifest,
        params=runtime.params,
        journal=getattr(runtime, "journal", None),
        calendar=cal,
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
    cal = calendar
    if cal is None:
        try:
            p_ctx = build_context(Mode.RESEARCH, run_id="screen_cal")
            cal = runtime.plugins.create(
                PluginKind.CALENDAR, "exchange", config={"exchange": "nse"}, ctx=p_ctx
            )
        except Exception:
            cal = getattr(data_service, "_calendar", None) if data_service else None
    cal = CachedCalendar.wrap(cal)

    ds = data_service or build_data_service(runtime, calendar=cal)
    data_adapter = DataServiceScreenAdapter(ds)
    indicator_resolver = PluginIndicatorResolver(runtime.plugins)

    manifest = getattr(getattr(ds, "_store", None), "manifest", None)
    if manifest is None:
        data_dir = getattr(getattr(runtime, "paths", None), "data", Path("data"))
        m_path = data_dir / "state" / "data_manifest.db"
        if not m_path.exists() and (data_dir / "manifest.db").exists():
            m_path = data_dir / "manifest.db"
        manifest = DataManifest(m_path) if m_path.exists() else None

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

    def session_resolver(end_d: Any, count: int) -> list[Any]:
        return resolve_trading_sessions(
            end_date=end_d,
            needed_sessions=count,
            calendar=cal,
            manifest=manifest,
            exchange="NSE",
        )

    return ScreenRunner(
        data_reader=data_adapter,
        indicator_resolver=indicator_resolver,
        calendar=cal,
        max_skipped_pct=skip_pct,
        progress_every=progress_every,
        params=getattr(runtime, "params", None),
        manifest=manifest,
        session_resolver=session_resolver,
    )


class RuntimeScreenService(ScreenService):
    """Execution implementation of ScreenService protocol in backtest mode."""

    def __init__(
        self,
        runtime: Runtime,
        screen_runner: ScreenRunner,
        clock: Clock,
        calendar: Calendar | None = None,
        mode: Mode = Mode.BACKTEST,
        journal: Any = None,
        manifest_callback: Callable[[str], None] | None = None,
        last_completed_session_fn: Callable[[], date | None] | None = None,
        run_config: Any = None,
    ) -> None:
        self.runtime = runtime
        self.screen_runner = screen_runner
        self.clock = clock
        self.calendar = calendar
        self.mode = mode
        self.journal = journal
        self.manifest_callback = manifest_callback
        self.last_completed_session_fn = last_completed_session_fn
        self.run_config = run_config

    def screen(self, name: str, *, group: str | None = None) -> ScreenSelection:
        """Run named screen as of the last completed session."""
        if self.mode != Mode.BACKTEST:
            raise NotSupported(
                what=f"Screen context call is not supported in {self.mode.value} mode",
                where="ScreenService.screen",
                fix="Screen context calls are only supported in backtest mode",
            )

        as_of: date | None = None
        if self.last_completed_session_fn is not None:
            as_of = self.last_completed_session_fn()
        else:
            now_dt = self.clock.now()
            ist_now = now_dt.astimezone(ZoneInfo("Asia/Kolkata"))
            cur_date = ist_now.date()
            if ist_now.time() >= time(15, 30) and (
                self.calendar is None or self.calendar.is_trading_day(cur_date)
            ):
                as_of = cur_date
            elif self.calendar is not None:
                walk_date = cur_date - timedelta(days=1)
                for _ in range(30):
                    try:
                        if self.calendar.is_trading_day(walk_date):
                            as_of = walk_date
                            break
                    except Exception:
                        pass
                    walk_date -= timedelta(days=1)

        if as_of is None:
            raise SafetyViolation(
                what="No trading session completed yet",
                where="ScreenService.screen",
                fix="Call screen only after at least one trading session has completed",
            )

        # 1. Load screen definition
        cfg_screens_dir = self.runtime.root / "config" / "screens"
        toml_path = cfg_screens_dir / f"{name}.toml"
        screen_obj: Any = None
        screen_file_hash: str | None = None
        if toml_path.is_file():
            from algodesk.screens.models import load_toml_screen

            screen_obj = load_toml_screen(toml_path)
            screen_file_hash = hashlib.sha256(toml_path.read_bytes()).hexdigest()
        else:
            try:
                p_ctx = build_context(Mode.RESEARCH, run_id=f"screen_{name}")
                plugin = self.runtime.plugins.create(PluginKind.SCREEN, name, config={}, ctx=p_ctx)
                p_info = self.runtime.plugins.info(PluginKind.SCREEN, name)
                screen_obj = plugin
                screen_file_hash = getattr(p_info, "code_hash", None)
            except Exception:
                pass

        if screen_obj is None:
            raise ConfigError(
                what=f"Screen '{name}' not found",
                where="ScreenService.screen",
                fix=f"Ensure config/screens/{name}.toml exists or install screen plugin '{name}'",
            )

        # 2. Resolve universe symbols
        from algodesk.data.groups import load_groups

        sym_list: list[str] = []
        groups_map = load_groups(self.runtime.root / "config" / "groups")
        target_group = (
            group
            or getattr(screen_obj, "universe", None)
            or getattr(getattr(self.run_config, "universe", None), "group", None)
        )
        if target_group and target_group in groups_map:
            sym_list = list(groups_map[target_group])
        elif group:
            raise ConfigError(
                what=f"Group '{group}' not found",
                where="ScreenService.screen",
                fix=f"Specify one of the available groups: {', '.join(sorted(groups_map.keys()))}",
            )
        else:
            if "nifty50" in groups_map:
                sym_list = list(groups_map["nifty50"])
            elif "demo5" in groups_map:
                sym_list = list(groups_map["demo5"])
            elif groups_map:
                sym_list = list(next(iter(groups_map.values())))
            else:
                sym_list = []

        # 3. Execute screen runner
        res: ScreenResult = self.screen_runner.run(
            screen=screen_obj,
            symbols=sym_list,
            as_of=as_of,
            group=target_group,
            adjust=getattr(screen_obj, "adjust", "splits"),
            screen_file_hash=screen_file_hash,
        )

        passed_symbols = tuple(r.symbol for r in res.rows if r.passed)
        scores = {r.symbol: float(r.score) for r in res.rows if r.passed}

        # 4. Journal universe.frozen (name, as_of, hash, count)
        if self.journal is not None and hasattr(self.journal, "append"):
            try:
                self.journal.append(
                    "universe.frozen",
                    {
                        "name": name,
                        "as_of": str(as_of),
                        "hash": res.result_hash,
                        "count": len(passed_symbols),
                        "symbols": list(passed_symbols),
                    },
                )
            except Exception:
                pass

        # 5. Record result hash via manifest callback
        if self.manifest_callback is not None:
            self.manifest_callback(res.result_hash)

        return ScreenSelection(
            symbols=passed_symbols,
            scores=scores,
            as_of=as_of,
            result_hash=res.result_hash,
        )
