"""Unified market data service with gap planning, rate limiting, and BarStore caching."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

from algodesk.api.errors import CalendarCoverageError, DataError, NotSupported
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import (
    ActionRecord,
    Bar,
    DataSlice,
    Instrument,
    InstrumentId,
    SeriesKey,
    parse_id,
)
from algodesk.api.protocols import Calendar, DataSource
from algodesk.api.services import JournalWriter, ParamReader
from algodesk.data.actions_store import ActionsStore
from algodesk.data.adjust import AdjustmentResult, AdjustmentView
from algodesk.data.fetcher import Fetcher
from algodesk.data.limiter import RateLimiter
from algodesk.data.planner import GapPlanner, resolve_base_timeframe
from algodesk.data.reconciler import CorporateActionsReconciler
from algodesk.data.resample import resample
from algodesk.data.staleness import check_and_update_adjusted_staleness
from algodesk.data.store import BarStore

_DEFAULT_EXCH_TZ = ZoneInfo("Asia/Kolkata")


def _get_reconciled(
    key: SeriesKey, store: BarStore, params: ParamReader | None
) -> list[ActionRecord]:
    parts = parse_id(key.instrument)
    raw = ActionsStore(data_dir=store.data_dir, manifest=store.manifest).read(
        parts.exchange, parts.symbol
    )
    if not raw:
        return []
    p_src = str(params.get("corp.primary_source", "upstox")) if params else "upstox"
    d_tol = float(params.get("corp.dividend_tolerance_pct", "1.0")) if params else 1.0
    return CorporateActionsReconciler(primary_source=p_src, dividend_tolerance_pct=d_tol).reconcile(
        parts.exchange, parts.symbol, raw
    )


def _check_staleness(
    key: SeriesKey,
    store: BarStore | None,
    sources: Mapping[str, DataSource],
    params: ParamReader | None,
    exchange_tz: tzinfo,
) -> None:
    if store is None:
        return
    if acts := _get_reconciled(key, store, params):
        check_and_update_adjusted_staleness(key, store, acts, sources, params, exchange_tz)


def _apply_raw_adjustments(
    bars: Sequence[Bar],
    key: SeriesKey,
    store: BarStore | None,
    from_basis: str,
    tgt_basis: str,
    accept_unverified: bool,
    params: ParamReader | None,
    exchange_tz: tzinfo,
) -> AdjustmentResult:
    if store is None or from_basis == tgt_basis:
        return AdjustmentResult(
            tuple(bars), [], "", factor_kinds=[], basis=tgt_basis, from_basis=from_basis
        )
    acts = _get_reconciled(key, store, params)
    d_key = SeriesKey(key.instrument, "bars", "1d", "raw", key.source)
    d_slice = store.read(d_key, datetime(2000, 1, 1, tzinfo=UTC), datetime.now(UTC))
    allow_unver_div = True
    if params is not None:
        try:
            val = params.get("corp.allow_unverified_dividends")
            if val is not None:
                allow_unver_div = val is True or str(val).lower() in ("true", "1", "yes")
        except Exception:
            allow_unver_div = True
    return AdjustmentView.apply(
        bars,
        acts,
        from_basis=from_basis,
        to_basis=tgt_basis,
        daily_bars=d_slice.bars or None,
        exchange_tz=exchange_tz,
        accept_unverified=accept_unverified,
        allow_unverified_dividends=allow_unver_div,
    )


class DataService:
    """Unified market data access layer routing to configured DataSources with caching."""

    __slots__ = (
        "_calendar",
        "_default_source",
        "_exchange_tz",
        "_journal",
        "_limiter",
        "_manifests",
        "_params",
        "_recorded_checksums",
        "_recorded_datasets",
        "_sources",
        "_store",
    )

    def __init__(
        self,
        sources: Mapping[str, DataSource],
        default_source: str = "yfinance",
        store: BarStore | None = None,
        limiter: RateLimiter | None = None,
        manifests: Mapping[str, PluginManifest] | None = None,
        calendar: Calendar | None = None,
        params: ParamReader | None = None,
        journal: JournalWriter | None = None,
        exchange_tz: tzinfo | None = None,
    ) -> None:
        self._sources = dict(sources)
        self._default_source = default_source
        self._store = store
        self._manifests = dict(manifests) if manifests else {}
        self._calendar = calendar
        self._params = params
        self._journal = journal
        self._exchange_tz = exchange_tz or _DEFAULT_EXCH_TZ
        self._limiter = limiter or RateLimiter(
            params=params,
            manifests=self._manifests,
            journal=journal,
        )
        self._recorded_datasets: set[str] = set()
        self._recorded_checksums: set[str] = set()

    @property
    def default_source(self) -> str:
        return self._default_source

    @property
    def store(self) -> BarStore | None:
        return self._store

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    def recorded_provenance(self) -> dict[str, list[str]]:
        """Return list of dataset IDs and checksums accessed through this service."""
        return {
            "datasets": sorted(self._recorded_datasets),
            "checksums": sorted(self._recorded_checksums),
        }

    def _resolve_source(self, name: str | None) -> DataSource:
        src_name = name or self._default_source
        if src_name not in self._sources:
            raise KeyError(
                f"Data source '{src_name}' not found. Configured: {list(self._sources.keys())}"
            )
        return self._sources[src_name]

    def history(
        self,
        key: SeriesKey,
        start: datetime,
        end: datetime,
        source: str | None = None,
    ) -> Sequence[Bar]:
        """Fetch historical bars for key between start and end (pass-through)."""
        ds = self._resolve_source(source or key.source)
        return list(ds.history(key, start, end))

    def bars(
        self,
        key: SeriesKey,
        start: datetime,
        end: datetime,
        source: str | None = None,
    ) -> Sequence[Bar]:
        """Alias for history (FR-DAT-1)."""
        return self.history(key, start, end, source=source)

    def instruments(self, source: str | None = None) -> Sequence[Instrument]:
        """Fetch tradable instruments from source."""
        ds = self._resolve_source(source)
        return list(ds.instruments())

    def _resolve_series_key(
        self, series: SeriesKey | str, source: str | None, download: str
    ) -> SeriesKey:
        if isinstance(series, SeriesKey):
            s_source = source or series.source or self._default_source
            return SeriesKey(
                instrument=series.instrument,
                kind=series.kind,
                timeframe=series.timeframe,
                adjustment=download,
                source=s_source,
            )
        s_str = str(series).strip()
        if s_str.count(":") >= 3:
            parts = s_str.split(":")
            inst = InstrumentId(":".join(parts[:3]))
            tf_str = parts[3]
        else:
            inst = InstrumentId(s_str if ":" in s_str else f"NSE:EQ:{s_str}")
            tf_str = "1m"
        s_source = source or self._default_source
        return SeriesKey(
            instrument=inst,
            kind="bars",
            timeframe=tf_str,
            adjustment=download,
            source=s_source,
        )

    def get(
        self,
        series: SeriesKey | str,
        start: datetime,
        end: datetime,
        download: str = "adjusted",
        adjust: str | None = None,
        fill_only: bool = False,
        source: str | None = None,
        accept_unverified: bool = False,
        presence_only: bool = False,
        refresh: bool = False,
    ) -> DataSlice:
        """Fetch historical bars with gap planning, rate limiting, and dynamic adjustment."""
        adj_mode = adjust if adjust is not None else "total"
        if adj_mode not in ("none", "splits", "total"):
            raise NotSupported(
                what=f"Dynamic adjustment '{adj_mode}' not supported yet",
                where="DataService.get",
                fix="Specify adjust='none', 'splits', or 'total'",
            )
        if download not in ("raw", "adjusted"):
            raise DataError(
                what=f"Invalid download mode '{download}'. Must be 'raw' or 'adjusted'",
                where="DataService.get",
                fix="Specify download='raw' or download='adjusted'",
            )
        if start > end:
            s_iso, e_iso = start.isoformat(), end.isoformat()
            raise CalendarCoverageError(
                what=f"Start timestamp ({s_iso}) must be <= end timestamp ({e_iso})",
                where="DataService.get",
                fix="Ensure start time precedes end time",
            )
        req_key = self._resolve_series_key(series, source, download)
        src_manifest = self._manifests.get(req_key.source)
        base_tf = resolve_base_timeframe(req_key.timeframe)
        base_key = SeriesKey(
            instrument=req_key.instrument,
            kind="bars",
            timeframe=base_tf,
            adjustment=download,
            source=req_key.source,
        )
        if self._store is None:
            raise DataError(
                what="BarStore is required for DataService.get",
                where="DataService.get",
                fix="Provide a configured BarStore to DataService",
            )

        ds_id = self._store.dataset_id(base_key)
        ds_rec = self._store.manifest.get_dataset(ds_id)
        if ds_rec and ds_rec.adjustment_basis:
            from_basis = ds_rec.adjustment_basis
        elif src_manifest is not None:
            from_basis = src_manifest.get_candles_adjustment_basis(download)
        elif req_key.source == "upstox":
            from_basis = "splits"
        elif req_key.source == "yfinance":
            from_basis = "total" if download == "adjusted" else "splits"
        elif req_key.source == "nse_bhavcopy":
            from_basis = "none"
        else:
            from_basis = "splits" if download == "raw" else "total"

        if from_basis == "unknown" and adj_mode == "total":
            raise DataError(
                what=f"Source '{req_key.source}' has unknown adjustment basis",
                where="DataService.get",
                fix="Use download='raw' with adjust='total' or verify source candle basis",
            )

        if download == "adjusted":
            _check_staleness(base_key, self._store, self._sources, self._params, self._exchange_tz)

        chunk_days = int(self._params.get("data.fetch_chunk_days", "30")) if self._params else 30
        plan = GapPlanner.plan(
            key=base_key,
            start=start,
            end=end,
            manifest=self._store.manifest,
            calendar=self._calendar,
            source_manifest=src_manifest,
            chunk_days=chunk_days,
            exchange_tz=self._exchange_tz,
            refresh=refresh,
        )
        if plan.has_work:
            Fetcher(
                store=self._store,
                limiter=self._limiter,
                sources=self._sources,
                params=self._params,
                journal=self._journal,
            ).fetch(plan)

        self._recorded_datasets.add(ds_id)
        now_iso = datetime.now(UTC).isoformat()
        if fill_only:
            return DataSlice(
                key=req_key,
                start=start,
                end=end,
                bars=(),
                gaps=(),
                dataset_ids=(ds_id,),
                checksums=(),
                source=req_key.source,
                adjustment=download,
                fetched_at=now_iso,
            )

        base_slice = self._store.read(base_key, start, end)
        for cs in base_slice.checksums:
            self._recorded_checksums.add(cs)
        raw_bars = (
            base_slice.bars
            if req_key.timeframe == base_tf
            else tuple(
                resample(
                    base_slice.bars,
                    req_key.timeframe,
                    calendar=self._calendar,
                    exchange_tz=self._exchange_tz,
                )
            )
        )
        if presence_only:
            return DataSlice(
                key=req_key,
                start=start,
                end=end,
                bars=raw_bars,
                gaps=base_slice.gaps,
                dataset_ids=(ds_id,),
                checksums=base_slice.checksums,
                source=req_key.source,
                adjustment=download,
                fetched_at=now_iso,
                provenance={
                    "dataset_ids": [ds_id],
                    "checksums": list(base_slice.checksums),
                    "source": req_key.source,
                    "adjustment": download,
                    "fetched_at": now_iso,
                    "presence_only": True,
                },
            )

        adj_res = _apply_raw_adjustments(
            bars=raw_bars,
            key=req_key,
            store=self._store,
            from_basis=from_basis,
            tgt_basis=adj_mode,
            accept_unverified=accept_unverified,
            params=self._params,
            exchange_tz=self._exchange_tz,
        )
        prov_dict: dict[str, object] = {
            "dataset_ids": [ds_id],
            "checksums": list(base_slice.checksums),
            "source": req_key.source,
            "adjustment": download,
            "fetched_at": now_iso,
            "action_ids": adj_res.action_ids,
            "actions_hash": adj_res.hash,
            "adjust": adj_mode,
            "basis": adj_mode,
            "adjustment_basis": adj_mode,
            "from_basis": from_basis,
            "factor_kinds": adj_res.factor_kinds,
        }
        prov_warnings: list[str] = []
        if req_key.source == "upstox":
            prov_warnings.append(
                "Upstox candle adjustment unknown; treated as adjusted pending verification"
            )
        if hasattr(adj_res, "warnings") and adj_res.warnings:
            prov_warnings.extend(adj_res.warnings)
        if prov_warnings:
            prov_dict["warnings"] = prov_warnings
        return DataSlice(
            key=req_key,
            start=start,
            end=end,
            bars=adj_res.bars,
            gaps=base_slice.gaps,
            dataset_ids=(ds_id,),
            checksums=base_slice.checksums,
            source=req_key.source,
            adjustment=download,
            fetched_at=now_iso,
            provenance=prov_dict,
        )
