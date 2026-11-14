"""Day-level coverage index and gap detection using the Calendar Protocol."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from algodesk.api.market import CoverageStatus, DataGap
from algodesk.api.protocols import Calendar
from algodesk.data.calendar_cache import CachedCalendar
from algodesk.data.closures import has_all_samples_evidence
from algodesk.data.manifest import CoverageDayRecord, DataManifest, MarketClosureRecord


class CoverageIndex:
    """Evaluates trading day completeness, settlement margins, and data gaps."""

    __slots__ = ("_calendar", "_manifest", "_settle_delay_m", "_last_closure_counts", "_params")

    def __init__(
        self,
        manifest: DataManifest,
        calendar: Calendar | None = None,
        settle_minutes: int | None = None,
        params: Any = None,
    ) -> None:
        self._manifest = manifest
        self._calendar = CachedCalendar.wrap(calendar)
        self._settle_delay_m = settle_minutes if settle_minutes is not None else 0
        self._params = params
        self._last_closure_counts: dict[str, int] = {
            "closed_calendar": 0,
            "closed_inferred": 0,
            "special_sessions": 0,
        }

    @property
    def calendar(self) -> Calendar | None:
        return self._calendar

    @property
    def settle_minutes(self) -> int:
        return self._settle_delay_m

    @property
    def params(self) -> Any:
        return self._params

    @property
    def last_closure_counts(self) -> dict[str, int]:
        return dict(self._last_closure_counts)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Allow CoverageIndex to be invoked safely as a callable.

        If called with no arguments (e.g. store.coverage_index()), returns self.
        If called with arguments, delegates to evaluate_day_status(*args, **kwargs).
        """
        if not args and not kwargs:
            return self
        return self.evaluate_day_status(*args, **kwargs)

    def is_empty_final(self, rec: CoverageDayRecord, now_dt: datetime | None = None) -> bool:
        """Check whether an existing EMPTY coverage record is final or stale."""
        return is_empty_record_final(
            rec=rec,
            calendar=self._calendar,
            settle_minutes=self._settle_delay_m,
            manifest=self._manifest,
            now_dt=now_dt,
            params=self._params,
        )

    def _resolve_exchange(self, dataset_id: str) -> str:
        u = dataset_id.upper()
        if "BSE" in u:
            return "BSE"
        if "MCX" in u:
            return "MCX"
        return "NSE"

    def evaluate_day_status(
        self,
        dataset_id: str,
        day: date,
        has_bars: bool,
        as_of: datetime | None = None,
    ) -> CoverageStatus:
        """Evaluate status (COMPLETE, PARTIAL, CLOSED, EMPTY) against calendar and settle margin."""
        now_utc = as_of or datetime.now(UTC)
        exch = self._resolve_exchange(dataset_id)
        day_str = day.isoformat()

        # Check market closures in manifest first
        if hasattr(self._manifest, "get_market_closure"):
            closure = self._manifest.get_market_closure(exch, day_str)
            if closure is not None:
                if closure.kind in ("inferred", "calendar"):
                    return CoverageStatus.CLOSED
                if closure.kind == "special":
                    # Special trading session is open
                    pass

        # 1. Check if calendar marks this day closed
        if self._calendar is not None:
            try:
                is_open = self._calendar.is_trading_day(day)
            except Exception:
                is_open = day.weekday() < 5
            if not is_open:
                return CoverageStatus.CLOSED
            try:
                sess_list = self._calendar.sessions(day)
                if not sess_list:
                    return CoverageStatus.CLOSED
                session_end = sess_list[-1].end
            except Exception:
                session_end = datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC)
        else:
            if day.weekday() >= 5:
                return CoverageStatus.CLOSED
            # Default NSE close is 15:30 IST (10:00 UTC)
            session_end = datetime(day.year, day.month, day.day, 10, 0, tzinfo=UTC)

        settle_deadline = session_end + timedelta(minutes=self._settle_delay_m)

        if not has_bars:
            if now_utc >= settle_deadline:
                return CoverageStatus.EMPTY
            return CoverageStatus.PARTIAL

        if now_utc >= settle_deadline:
            return CoverageStatus.COMPLETE

        return CoverageStatus.PARTIAL

    def detect_gaps(
        self,
        dataset_id: str,
        start_day: date,
        end_day: date,
    ) -> list[DataGap]:
        """Detect missing or incomplete days across expected trading sessions."""
        cov_records = {
            r.day: r
            for r in self._manifest.get_coverage_days(
                dataset_id,
                start_day=start_day.isoformat(),
                end_day=end_day.isoformat(),
            )
        }

        exch = self._resolve_exchange(dataset_id)
        closures_records: dict[str, MarketClosureRecord] = {}
        if hasattr(self._manifest, "list_market_closures"):
            for mc in self._manifest.list_market_closures(
                exchange=exch,
                start_day=start_day.isoformat(),
                end_day=end_day.isoformat(),
            ):
                closures_records[mc.day] = mc

        gaps: list[DataGap] = []
        cur = start_day
        one_day = timedelta(days=1)

        closed_cal_count = 0
        closed_inf_count = 0
        special_count = 0

        while cur <= end_day:
            day_str = cur.isoformat()
            closure_rec = closures_records.get(day_str)
            closure_kind = closure_rec.kind if closure_rec else None

            is_closed = False
            if closure_kind == "special":
                special_count += 1
                is_closed = False
            elif closure_kind == "inferred":
                if has_all_samples_evidence(closure_rec):
                    closed_inf_count += 1
                    is_closed = True
                else:
                    is_closed = False
            elif closure_kind in ("calendar", "exchange_file"):
                closed_cal_count += 1
                is_closed = True
            else:
                if self._calendar is not None:
                    try:
                        if not self._calendar.is_trading_day(cur):
                            is_closed = True
                            closed_cal_count += 1
                    except Exception:
                        if cur.weekday() >= 5:
                            is_closed = True
                            closed_cal_count += 1
                elif cur.weekday() >= 5:
                    is_closed = True
                    closed_cal_count += 1

            if not is_closed:
                rec = cov_records.get(day_str)
                if rec is None:
                    gaps.append(
                        DataGap(
                            day=cur,
                            status=CoverageStatus.MISSING,
                            reason="No coverage record in manifest",
                        )
                    )
                elif rec.status == CoverageStatus.EMPTY:
                    final, reason = evaluate_empty_record(
                        rec=rec,
                        calendar=self._calendar,
                        settle_minutes=self._settle_delay_m,
                        manifest=self._manifest,
                        params=self._params,
                    )
                    if final:
                        gaps.append(
                            DataGap(
                                day=cur,
                                status=CoverageStatus.EMPTY,
                                reason=rec.note or "Fetched and the source returned nothing",
                            )
                        )
                    else:
                        gaps.append(
                            DataGap(
                                day=cur,
                                status=CoverageStatus.MISSING,
                                reason=f"Stale EMPTY record ({reason})",
                            )
                        )
                elif rec.status == "PENDING":
                    gaps.append(
                        DataGap(
                            day=cur,
                            status=CoverageStatus.MISSING,
                            reason=rec.note or "Pending next-day history retry",
                        )
                    )
                elif rec.status not in (CoverageStatus.COMPLETE, CoverageStatus.CLOSED):
                    try:
                        gap_st = CoverageStatus(rec.status)
                    except ValueError:
                        gap_st = CoverageStatus.MISSING
                    gaps.append(
                        DataGap(
                            day=cur,
                            status=gap_st,
                            reason=rec.note or f"Day status is {rec.status}",
                        )
                    )

            cur += one_day

        self._last_closure_counts = {
            "closed_calendar": closed_cal_count,
            "closed_inferred": closed_inf_count,
            "special_sessions": special_count,
        }

        return gaps


def evaluate_empty_record(
    rec: CoverageDayRecord,
    calendar: Calendar | None = None,
    settle_minutes: int | None = None,
    manifest: DataManifest | None = None,
    now_dt: datetime | None = None,
    params: Any = None,
) -> tuple[bool, str]:
    """Evaluate whether an EMPTY coverage record is final or stale/invalid.

    EMPTY is final only when:
    1. The fetch ran after the session ended plus data.settle_minutes.
    2. The response was a successful empty chunk for that exact IST date window.
    3. The source actually serves same-day history (or fetch was on a later date).
    """
    if rec.status == "PENDING":
        return False, "Pending next-day history retry"
    if rec.status != CoverageStatus.EMPTY:
        return True, "Not EMPTY status"
    if not rec.fetched_at:
        return False, "Fetch timestamp missing"

    calendar = CachedCalendar.wrap(calendar)

    try:
        fa = datetime.fromisoformat(rec.fetched_at)
        fa_utc = fa.astimezone(UTC) if fa.tzinfo else fa.replace(tzinfo=UTC)
    except Exception:
        return False, f"Invalid fetched_at timestamp: {rec.fetched_at}"

    # Check same-day history policy for the source
    parts = rec.dataset_id.split(":")
    source = parts[1].lower() if len(parts) >= 2 else ""
    same_day_served = True
    if params is not None:
        try:
            val = params.get(f"source.{source}.same_day_history")
            if val is not None:
                if isinstance(val, bool):
                    same_day_served = val
                elif isinstance(val, str):
                    same_day_served = val.strip().lower() in ("true", "1", "yes")
                else:
                    same_day_served = bool(val)
        except Exception:
            same_day_served = source != "upstox"
    elif source == "upstox":
        same_day_served = False

    day_d = date.fromisoformat(rec.day)
    session_end: datetime | None = None
    if calendar is not None:
        try:
            sess_list = calendar.sessions(day_d)
            if sess_list:
                session_end = sess_list[-1].end.astimezone(UTC)
        except Exception:
            pass

    if session_end is None:
        session_end = datetime(day_d.year, day_d.month, day_d.day, 10, 0, tzinfo=UTC)

    settle_delay_m = settle_minutes if settle_minutes is not None else 0
    settle_deadline = session_end + timedelta(minutes=settle_delay_m)
    if fa_utc < settle_deadline:
        return False, f"Fetched before session end + {settle_delay_m}m settle margin"

    tf = parts[2].lower() if len(parts) >= 3 else ""
    if not same_day_served and tf not in ("1d", "daily"):
        from zoneinfo import ZoneInfo

        fa_ist = fa_utc.astimezone(ZoneInfo("Asia/Kolkata"))
        if day_d == fa_ist.date():
            return False, f"Same-day history not served by source {source}"

    note_low = (rec.note or "").lower()
    if any(
        w in note_low
        for w in (
            "object is not callable",
            "traceback",
            "exception",
            "crash",
            "typeerror",
            "attributeerror",
            "valueerror",
        )
    ):
        return False, f"Fetch error: {rec.note}"

    if len(parts) >= 3:
        tf = parts[2].lower()
        if source == "yfinance" and tf in ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"):
            cutoff = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
            if fa_utc < cutoff:
                return False, "Old yfinance intraday date window (pre-M2-11)"

    if manifest is not None:
        if hasattr(manifest, "check_fetch_log_ok"):
            ok, err = manifest.check_fetch_log_ok(rec.fetched_at)
            if not ok:
                return False, f"Fetch log error: {err}"
        elif hasattr(manifest, "_conn"):
            try:
                cur = manifest._conn.execute(
                    "SELECT outcome, error FROM fetch_log WHERE finished = ?", (rec.fetched_at,)
                )
                for row in cur.fetchall():
                    if row["outcome"] != "OK" or (row["error"] and row["error"].strip()):
                        return False, f"Fetch log error: {row['error'] or row['outcome']}"
            except Exception:
                pass

    return True, "Final EMPTY chunk"


def is_empty_record_final(
    rec: CoverageDayRecord,
    calendar: Calendar | None = None,
    settle_minutes: int | None = None,
    manifest: DataManifest | None = None,
    now_dt: datetime | None = None,
    params: Any = None,
) -> bool:
    """Return True if the EMPTY record is validly final, False if stale/invalid."""
    is_final, _ = evaluate_empty_record(
        rec=rec,
        calendar=calendar,
        settle_minutes=settle_minutes,
        manifest=manifest,
        now_dt=now_dt,
        params=params,
    )
    return is_final


def reset_stale_empty_coverage(
    manifest: DataManifest,
    calendar: Calendar | None = None,
    settle_minutes: int | None = None,
    source: str | None = None,
    timeframe: str | None = None,
    dry_run: bool = False,
    journal: Any = None,
    params: Any = None,
) -> list[tuple[CoverageDayRecord, str]]:
    """Re-evaluate existing EMPTY coverage records and reset non-final ones to MISSING."""
    if not hasattr(manifest, "_conn"):
        return []

    cur = manifest._conn.execute(
        "SELECT * FROM coverage_days WHERE status IN ('EMPTY', 'PENDING') ORDER BY dataset_id, day"
    )
    rows = cur.fetchall()

    to_reset: list[tuple[CoverageDayRecord, str]] = []
    for r in rows:
        rec = CoverageDayRecord(**dict(r))
        parts = rec.dataset_id.split(":")
        r_source = parts[1].lower() if len(parts) >= 2 else ""
        r_tf = parts[2].lower() if len(parts) >= 3 else ""

        if source and r_source != source.strip().lower():
            continue
        if timeframe and r_tf != timeframe.strip().lower():
            continue

        is_final, reason = evaluate_empty_record(
            rec=rec,
            calendar=calendar,
            settle_minutes=settle_minutes,
            manifest=manifest,
            params=params,
        )
        if not is_final:
            to_reset.append((rec, reason))

    if not dry_run and to_reset:
        updated: list[CoverageDayRecord] = [
            CoverageDayRecord(
                dataset_id=rec.dataset_id,
                day=rec.day,
                status=CoverageStatus.MISSING,
                rows=0,
                expected_rows=rec.expected_rows,
                part_path="",
                fetched_at=rec.fetched_at,
                note=f"Reset from stale EMPTY: {reason}",
            )
            for rec, reason in to_reset
        ]
        manifest.record_coverage_days(updated)

        if journal is not None:
            journal.append(
                kind="data.coverage_reset_empty",
                payload={
                    "source": source or "all",
                    "timeframe": timeframe or "all",
                    "reset_count": len(to_reset),
                    "records": [
                        {
                            "dataset_id": rec.dataset_id,
                            "day": rec.day,
                            "reason": reason,
                            "fetched_at": rec.fetched_at,
                        }
                        for rec, reason in to_reset
                    ],
                },
                durable=True,
            )

    return to_reset


@dataclass(frozen=True)
class CoverageRepairItem:
    """Record of a coverage day repair action re-derived from parquet bars."""

    dataset_id: str
    day: str
    old_status: str
    new_status: str
    old_rows: int
    new_rows: int
    part_path: str
    note: str


def repair_coverage(
    manifest: DataManifest,
    data_dir: Path,
    calendar: Calendar | None = None,
    source: str | None = None,
    timeframe: str | None = None,
    dry_run: bool = False,
    journal: Any = None,
    now_dt: datetime | None = None,
) -> list[CoverageRepairItem]:
    """Re-derive coverage_days from stored parquet files.

    If bars are present in stored parquet and the session is complete,
    ensures status is COMPLETE with accurate row count, journaling any changes.
    """
    from zoneinfo import ZoneInfo

    import pyarrow.parquet as pq

    exch_tz = ZoneInfo("Asia/Kolkata")
    now_ist = (now_dt or datetime.now(UTC)).astimezone(exch_tz)
    today = now_ist.date()

    datasets = manifest.list_datasets(kind="bars")
    repaired_items: list[CoverageRepairItem] = []
    to_update_records: list[CoverageDayRecord] = []

    for ds in datasets:
        parts = ds.dataset_id.split(":")
        r_source = parts[1].lower() if len(parts) >= 2 else ""
        r_tf = parts[2].lower() if len(parts) >= 3 else ""

        if source and r_source != source.strip().lower():
            continue
        if timeframe and r_tf != timeframe.strip().lower():
            continue

        existing_cov = {r.day: r for r in manifest.get_coverage_days(ds.dataset_id)}
        ds_parts = manifest.get_parts(ds.dataset_id)
        if not ds_parts:
            continue

        day_counts: dict[str, int] = {}
        day_paths: dict[str, str] = {}

        for p in ds_parts:
            p_full = data_dir / p.path if not Path(p.path).is_absolute() else Path(p.path)
            if not p_full.exists():
                continue
            try:
                tbl = pq.read_table(p_full, columns=["start_ts"])
                if "start_ts" not in tbl.column_names or tbl.num_rows == 0:
                    continue
                for ts_val in tbl.column("start_ts").to_pylist():
                    if isinstance(ts_val, datetime):
                        ts_ist = (
                            ts_val.astimezone(exch_tz)
                            if ts_val.tzinfo
                            else ts_val.replace(tzinfo=UTC).astimezone(exch_tz)
                        )
                        d_str = ts_ist.strftime("%Y-%m-%d")
                        day_counts[d_str] = day_counts.get(d_str, 0) + 1
                        day_paths[d_str] = p.path
            except Exception:
                continue

        for d_str, count in sorted(day_counts.items()):
            if count <= 0:
                continue
            d_date = date.fromisoformat(d_str)

            is_complete_session = False
            if d_date < today:
                is_complete_session = True
            elif d_date == today:
                if calendar is not None:
                    try:
                        s_list = calendar.sessions(d_date)
                        if (
                            s_list
                            and hasattr(s_list[-1], "end")
                            and isinstance(s_list[-1].end, datetime)
                        ):
                            sess_end = s_list[-1].end.astimezone(exch_tz)
                            if now_ist >= sess_end:
                                is_complete_session = True
                    except Exception:
                        pass
                if not is_complete_session and now_ist.time() >= time(15, 30):
                    is_complete_session = True

            if not is_complete_session:
                continue

            old_rec = existing_cov.get(d_str)
            old_st = old_rec.status if old_rec else "MISSING"
            old_rows = old_rec.rows if old_rec else 0
            old_path = old_rec.part_path if old_rec else ""

            if str(old_st) == CoverageStatus.CLOSED or str(old_st) == "CLOSED":
                is_special = False
                if calendar is not None and hasattr(calendar, "is_special"):
                    try:
                        is_special = bool(calendar.is_special(d_date))
                    except Exception:
                        is_special = False
                has_disagreement = False
                if hasattr(manifest, "get_market_closure"):
                    exch = (
                        ds.dataset_id.split(":")[3] if len(ds.dataset_id.split(":")) > 3 else "NSE"
                    )
                    cl = manifest.get_market_closure(exch, d_str)
                    if cl is not None and cl.kind == "inferred":
                        has_disagreement = True
                if not is_special and not has_disagreement:
                    continue

            if old_st != CoverageStatus.COMPLETE or old_rows != count:
                rep_item = CoverageRepairItem(
                    dataset_id=ds.dataset_id,
                    day=d_str,
                    old_status=str(old_st),
                    new_status=CoverageStatus.COMPLETE,
                    old_rows=old_rows,
                    new_rows=count,
                    part_path=day_paths.get(d_str, old_path),
                    note="Re-derived from stored parquet bars",
                )
                repaired_items.append(rep_item)
                to_update_records.append(
                    CoverageDayRecord(
                        dataset_id=ds.dataset_id,
                        day=d_str,
                        status=CoverageStatus.COMPLETE,
                        rows=count,
                        expected_rows=count,
                        part_path=day_paths.get(d_str, old_path),
                        fetched_at=(
                            old_rec.fetched_at
                            if old_rec and old_rec.fetched_at
                            else now_ist.astimezone(UTC).isoformat()
                        ),
                        note="",
                    )
                )

    if not dry_run and to_update_records:
        manifest.record_coverage_days(to_update_records)
        if journal is not None:
            journal.append(
                kind="data.coverage.repair",
                payload={
                    "source": source or "all",
                    "timeframe": timeframe or "all",
                    "repair_count": len(repaired_items),
                    "items": [asdict(item) for item in repaired_items],
                },
                durable=True,
            )

    return repaired_items


@dataclass
class PruneReport:
    """Report of pruned coverage, dataset, and fetch log records."""

    pruned_datasets: list[str]
    deleted_coverage_rows: int
    deleted_fetch_log_rows: int
    dry_run: bool


def prune_unmappable_coverage(
    manifest: DataManifest,
    dry_run: bool = True,
    journal: Any = None,
) -> PruneReport:
    """Delete coverage and dataset rows that have no stored files and an unmappable instrument.

    Identifies orphan datasets (such as test runs with NONEXISTENT_SYMBOL) where no parquet
    files or parts exist on disk, and purges them from coverage_days, datasets, and fetch_log.
    """
    conn = manifest._conn
    cursor = conn.cursor()

    # Query all datasets and distinct coverage dataset IDs
    cursor.execute("SELECT dataset_id, instrument FROM datasets")
    ds_map = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute("SELECT DISTINCT dataset_id FROM coverage_days")
    all_dataset_ids = sorted(list(set(ds_map.keys()) | {row[0] for row in cursor.fetchall()}))

    target_datasets: list[str] = []
    target_instruments: set[str] = set()

    for ds_id in all_dataset_ids:
        inst_str = ds_map.get(ds_id, "")
        if not inst_str:
            # Extract instrument from dataset_id convention: kind:source:tf:EXCH:SEG:SYM:adj
            parts = ds_id.split(":")
            if len(parts) >= 6:
                inst_str = f"{parts[3]}:{parts[4]}:{parts[5]}"
            elif len(parts) >= 4:
                inst_str = parts[3]

        # Check if stored parts or files exist
        cursor.execute("SELECT count(*) FROM parts WHERE dataset_id = ?", (ds_id,))
        p_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM files WHERE instrument = ?", (inst_str,))
        f_count = cursor.fetchone()[0]
        if p_count > 0 or f_count > 0:
            continue

        # Check if there are COMPLETE coverage rows
        cursor.execute(
            "SELECT count(*) FROM coverage_days WHERE dataset_id = ? AND status = 'COMPLETE'",
            (ds_id,),
        )
        c_count = cursor.fetchone()[0]
        if c_count > 0:
            continue

        # Check if instrument is unmappable:
        # Notes in coverage_days mentioning unknown/cannot map, or NONEXISTENT in identifier
        upper_inst = str(inst_str).upper()
        upper_id = str(ds_id).upper()
        is_unmappable = (
            "NONEXISTENT" in upper_inst or "NONEXISTENT" in upper_id or "DUMMY" in upper_inst
        )
        if not is_unmappable:
            cursor.execute(
                "SELECT count(*) FROM coverage_days WHERE dataset_id = ? AND ("
                "note LIKE '%Cannot map unknown symbol%' OR note LIKE '%unknown symbol%')",
                (ds_id,),
            )
            note_match = cursor.fetchone()[0]
            if note_match > 0:
                is_unmappable = True

        if is_unmappable:
            target_datasets.append(ds_id)
            if inst_str:
                target_instruments.add(inst_str)

    # Count affected rows
    cov_rows_count = 0
    fetch_log_count = 0
    for ds_id in target_datasets:
        cursor.execute("SELECT count(*) FROM coverage_days WHERE dataset_id = ?", (ds_id,))
        cov_rows_count += cursor.fetchone()[0]

    for inst_str in target_instruments:
        cursor.execute("SELECT count(*) FROM fetch_log WHERE chunk LIKE ?", (f"{inst_str}%",))
        fetch_log_count += cursor.fetchone()[0]

    if not dry_run and target_datasets:
        for ds_id in target_datasets:
            cursor.execute("DELETE FROM coverage_days WHERE dataset_id = ?", (ds_id,))
            cursor.execute("DELETE FROM datasets WHERE dataset_id = ?", (ds_id,))

        for inst_str in target_instruments:
            cursor.execute("DELETE FROM fetch_log WHERE chunk LIKE ?", (f"{inst_str}%",))

        conn.commit()

        if journal is not None:
            try:
                journal.append(
                    kind="data.coverage.prune",
                    payload={
                        "pruned_datasets": target_datasets,
                        "deleted_coverage_rows": cov_rows_count,
                        "deleted_fetch_log_rows": fetch_log_count,
                    },
                    durable=True,
                )
            except Exception:
                pass

    return PruneReport(
        pruned_datasets=target_datasets,
        deleted_coverage_rows=cov_rows_count,
        deleted_fetch_log_rows=fetch_log_count,
        dry_run=dry_run,
    )
