"""Parallel, rate-limited historical data chunk fetcher with transactional storage."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from algodesk.api.errors import DataError, RateLimitError
from algodesk.api.market import CoverageStatus, SeriesKey
from algodesk.api.protocols import DataSource
from algodesk.api.services import JournalWriter, ParamReader
from algodesk.data.limiter import RateLimiter
from algodesk.data.manifest import CoverageDayRecord
from algodesk.data.planner import FetchChunk, FetchPlan
from algodesk.data.store import BarStore
from algodesk.data.validator import BarValidator


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of fetching an individual chunk."""

    chunk: FetchChunk
    status: str
    rows: int
    error: str = ""


class Fetcher:
    """Fetches plan chunks across a thread pool honoring per-endpoint rate limits."""

    __slots__ = (
        "_attempts",
        "_journal",
        "_limiter",
        "_lock",
        "_params",
        "_sources",
        "_store",
        "_validator",
        "_workers",
    )

    def __init__(
        self,
        store: BarStore,
        limiter: RateLimiter,
        sources: Mapping[str, DataSource],
        validator: BarValidator | None = None,
        params: ParamReader | None = None,
        journal: JournalWriter | None = None,
        max_workers: int | None = None,
        retries: int | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._store = store
        self._limiter = limiter
        self._sources = dict(sources)
        self._params = params
        self._journal = journal

        if validator is not None:
            self._validator = validator
        else:
            self._validator = BarValidator(
                data_dir=store.data_dir,
                manifest=store.manifest,
                calendar=store.coverage_index.calendar,
                params=params,
                journal=journal,
            )

        def _p_int(k: str, default: int) -> int:
            try:
                return int(params.get(k)) if params else default
            except Exception:
                return default

        self._workers = max(
            1, max_workers if max_workers is not None else _p_int("data.fetch_max_workers", 1)
        )
        self._attempts = max(0, retries if retries is not None else _p_int("data.fetch_retries", 1))

    def _resolve_source(self, name: str) -> DataSource:
        if name not in self._sources:
            raise DataError(
                what=f"DataSource '{name}' not found among configured sources",
                where="Fetcher._resolve_source",
                fix=f"Configure '{name}' in runtime data sources: {list(self._sources.keys())}",
            )
        return self._sources[name]

    def _fetch_single_chunk(
        self,
        chunk: FetchChunk,
        base_key: SeriesKey,
    ) -> FetchResult:
        source_name = base_key.source
        ds = self._resolve_source(source_name)
        attempts = 0
        max_attempts = self._attempts + 1
        last_error = ""

        manifest = self._store.manifest
        while attempts < max_attempts:
            started_iso = datetime.now(UTC).isoformat()
            self._limiter.acquire(source_name, "history")
            bars: list[Any] = []
            try:
                raw_bars = ds.history(base_key, chunk.start, chunk.end)
                bars = list(raw_bars)
                self._limiter.record_success(source_name, "history")

                with self._lock:
                    # Validate bars
                    val_rep = self._validator.validate_chunk(bars, base_key)
                    clean_bars = (
                        val_rep.clean_bars
                        if hasattr(val_rep, "clean_bars") and val_rep.clean_bars is not None
                        else bars
                    )
                    if not clean_bars and getattr(val_rep, "dropped_flat_bars", 0) > 0:
                        # pyrefly: ignore[implicit-any-empty-container]
                        clean_bars = []

                    # Commit to BarStore
                    parts = self._store.commit(
                        base_key, clean_bars, refresh=False, validator=self._validator
                    )
                    finished_iso = datetime.now(UTC).isoformat()
                    ds_id = self._store.dataset_id(base_key)

                    # Expected sessions minus returned days become EMPTY, never COMPLETE
                    returned_days = {
                        b.start_ts.astimezone(self._store.exchange_tz).date() for b in clean_bars
                    }
                    missing_chunk_days = [d for d in chunk.days if d not in returned_days]
                    cov_idx = (
                        self._store.coverage_index
                        if hasattr(self._store, "coverage_index")
                        else None
                    )
                    now_check = datetime.now(UTC)
                    cov_empty = []
                    for d in missing_chunk_days:
                        c_status = CoverageStatus.EMPTY
                        c_note = "Expected session returned no bars from source"
                        cal = getattr(cov_idx, "calendar", None) if cov_idx else None
                        try:
                            settle_m = int(getattr(cov_idx, "settle_minutes", 0))
                        except (TypeError, ValueError):
                            settle_m = 0
                        if cov_idx is not None:
                            try:
                                eval_st = cov_idx.evaluate_day_status(
                                    ds_id, d, has_bars=False, as_of=now_check
                                )
                                if eval_st in (CoverageStatus.PARTIAL, CoverageStatus.CLOSED):
                                    c_status = eval_st
                                    c_note = (
                                        "Current session in progress / incomplete"
                                        if eval_st == CoverageStatus.PARTIAL
                                        else "Session closed"
                                    )
                            except Exception:
                                pass
                        # Ensure EMPTY is never assigned before session close + settle margin
                        if c_status == CoverageStatus.EMPTY:
                            sess_end = None
                            if cal is not None:
                                try:
                                    s_list = cal.sessions(d)
                                    if s_list:
                                        last_s = s_list[-1]
                                        if hasattr(last_s, "end") and isinstance(
                                            last_s.end, datetime
                                        ):
                                            sess_end = last_s.end.astimezone(UTC)
                                except Exception:
                                    pass
                            if not isinstance(sess_end, datetime):
                                sess_end = datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC)
                            deadline = sess_end + timedelta(minutes=settle_m)
                            if now_check < deadline:
                                c_status = CoverageStatus.PARTIAL
                                c_note = "Current session in progress / incomplete"
                        cov_empty.append(
                            CoverageDayRecord(
                                dataset_id=ds_id,
                                day=d.isoformat(),
                                status=c_status,
                                rows=0,
                                expected_rows=0,
                                part_path="",
                                fetched_at=finished_iso,
                                note=c_note,
                            )
                        )
                    if cov_empty:
                        manifest.record_coverage_days(cov_empty)

                    manifest.record_fetch_log(
                        chunk=chunk.chunk_id,
                        source=source_name,
                        started=started_iso,
                        finished=finished_iso,
                        rows=len(bars),
                        outcome="OK",
                        error="",
                    )

                    if self._journal is not None:
                        self._journal.append(
                            kind="data.fetch",
                            payload={
                                "chunk": chunk.chunk_id,
                                "source": source_name,
                                "rows": len(bars),
                                "status": "OK",
                                "parts": [p.path for p in parts],
                            },
                        )

                return FetchResult(chunk=chunk, status="OK", rows=len(bars))

            except Exception as exc:
                last_error = str(exc)
                is_val_fail = (
                    "validation failed" in last_error.lower()
                    or "quarantine" in last_error.lower()
                    or "quarantined" in last_error.lower()
                    or (isinstance(exc, DataError) and "validation" in last_error.lower())
                )
                if is_val_fail:
                    finished_iso = datetime.now(UTC).isoformat()
                    with self._lock:
                        manifest.record_fetch_log(
                            chunk=chunk.chunk_id,
                            source=source_name,
                            started=started_iso,
                            finished=finished_iso,
                            rows=len(bars),
                            outcome="QUARANTINED",
                            error=last_error,
                        )
                        if self._journal is not None:
                            self._journal.append(
                                kind="data.fetch",
                                payload={
                                    "chunk": chunk.chunk_id,
                                    "source": source_name,
                                    "rows": len(bars),
                                    "status": "QUARANTINED",
                                    "error": last_error,
                                },
                            )
                    return FetchResult(
                        chunk=chunk,
                        status="QUARANTINED",
                        rows=len(bars),
                        error=last_error,
                    )

                if (
                    "429" in last_error
                    or "too many requests" in last_error.lower()
                    or isinstance(exc, RateLimitError)
                ):
                    self._limiter.record_rate_error(source_name, "history", exc=exc)

                attempts += 1

        # All retries exhausted -> mark EMPTY with the error
        finished_iso = datetime.now(UTC).isoformat()
        with self._lock:
            manifest.record_fetch_log(
                chunk=chunk.chunk_id,
                source=source_name,
                started=started_iso,
                finished=finished_iso,
                rows=0,
                outcome="EMPTY",
                error=last_error,
            )

            ds_id = self._store.dataset_id(base_key)
            cov_updates = [
                CoverageDayRecord(
                    dataset_id=ds_id,
                    day=d.isoformat(),
                    status=CoverageStatus.EMPTY,
                    rows=0,
                    expected_rows=0,
                    part_path="",
                    fetched_at=finished_iso,
                    note=f"Fetch failed after {self._attempts} retries: {last_error}",
                )
                for d in chunk.days
            ]
            manifest.record_coverage_days(cov_updates)

            if self._journal is not None:
                self._journal.append(
                    kind="data.fetch",
                    payload={
                        "chunk": chunk.chunk_id,
                        "source": source_name,
                        "rows": 0,
                        "status": "EMPTY",
                        "error": last_error,
                    },
                )

        return FetchResult(chunk=chunk, status="EMPTY", rows=0, error=last_error)

    def fetch(self, plan: FetchPlan) -> list[FetchResult]:
        """Execute parallel fetching for all chunks in plan."""
        if not plan.chunks:
            return []

        results: list[FetchResult] = []
        if len(plan.chunks) == 1 or self._workers == 1:
            for c in plan.chunks:
                results.append(self._fetch_single_chunk(c, plan.base_key))
            return results

        with ThreadPoolExecutor(max_workers=min(self._workers, len(plan.chunks))) as executor:
            future_to_chunk = {
                executor.submit(self._fetch_single_chunk, c, plan.base_key): c for c in plan.chunks
            }
            for fut in as_completed(future_to_chunk):
                results.append(fut.result())

        return results
