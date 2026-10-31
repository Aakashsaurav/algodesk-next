"""Unified market data service with gap planning, rate limiting, and BarStore caching."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

from algodesk.api.errors import CalendarCoverageError, DataError, NotSupported
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, DataSlice, Instrument, InstrumentId, SeriesKey
from algodesk.api.protocols import Calendar, DataSource
from algodesk.api.services import JournalWriter, ParamReader
from algodesk.data.fetcher import Fetcher
from algodesk.data.limiter import RateLimiter
from algodesk.data.planner import GapPlanner, resolve_base_timeframe
from algodesk.data.resample import resample
from algodesk.data.store import BarStore

_DEFAULT_EXCH_TZ = ZoneInfo("Asia/Kolkata")


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

    def get(
        self,
        series: SeriesKey | str,
        start: datetime,
        end: datetime,
        download: str = "adjusted",
        adjust: str = "none",
        fill_only: bool = False,
        source: str | None = None,
    ) -> DataSlice:
        """Fetch historical bars with gap planning, rate limiting, and resampling."""
        if adjust != "none":
            raise NotSupported(
                what=f"Dynamic adjustment '{adjust}' is not supported yet (requires M2-4)",
                where="DataService.get",
                fix="Use adjust='none' with download='raw' or download='adjusted'",
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

        # Resolve SeriesKey
        if isinstance(series, SeriesKey):
            s_source = source or series.source or self._default_source
            req_key = SeriesKey(
                instrument=series.instrument,
                kind=series.kind,
                timeframe=series.timeframe,
                adjustment=download,
                source=s_source,
            )
        else:
            s_str = str(series).strip()
            if s_str.count(":") >= 3:
                parts = s_str.split(":")
                inst = InstrumentId(":".join(parts[:3]))
                tf_str = parts[3]
            else:
                inst = InstrumentId(s_str if ":" in s_str else f"NSE:EQ:{s_str}")
                tf_str = "1m"
            s_source = source or self._default_source
            req_key = SeriesKey(
                instrument=inst,
                kind="bars",
                timeframe=tf_str,
                adjustment=download,
                source=s_source,
            )

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

        chunk_days = 30
        if self._params is not None:
            try:
                chunk_days = int(self._params.get("data.fetch_chunk_days"))
            except Exception:
                pass

        src_manifest = self._manifests.get(base_key.source)
        plan = GapPlanner.plan(
            key=base_key,
            start=start,
            end=end,
            manifest=self._store.manifest,
            calendar=self._calendar,
            source_manifest=src_manifest,
            chunk_days=chunk_days,
            exchange_tz=self._exchange_tz,
        )

        if plan.has_work:
            fetcher = Fetcher(
                store=self._store,
                limiter=self._limiter,
                sources=self._sources,
                params=self._params,
                journal=self._journal,
            )
            fetcher.fetch(plan)

        ds_id = self._store.dataset_id(base_key)
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

        # Read base series from store
        base_slice = self._store.read(base_key, start, end)
        for cs in base_slice.checksums:
            self._recorded_checksums.add(cs)

        # Resample if requested timeframe differs from base timeframe
        if req_key.timeframe == base_tf:
            bars = base_slice.bars
        else:
            resampled = resample(
                base_slice.bars,
                req_key.timeframe,
                calendar=self._calendar,
                exchange_tz=self._exchange_tz,
            )
            bars = tuple(resampled)

        prov_dict: dict[str, object] = {
            "dataset_ids": [ds_id],
            "checksums": list(base_slice.checksums),
            "source": req_key.source,
            "adjustment": download,
            "fetched_at": now_iso,
        }
        if req_key.source == "upstox":
            prov_dict["warnings"] = [
                "Upstox candle adjustment unknown; treated as adjusted pending verification"
            ]

        return DataSlice(
            key=req_key,
            start=start,
            end=end,
            bars=bars,
            gaps=base_slice.gaps,
            dataset_ids=(ds_id,),
            checksums=base_slice.checksums,
            source=req_key.source,
            adjustment=download,
            fetched_at=now_iso,
            provenance=prov_dict,
        )
