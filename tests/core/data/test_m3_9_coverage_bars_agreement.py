"""Tests for M3-9: Coverage and stored bars agreement, concurrency, verify, and repair."""

from __future__ import annotations

import concurrent.futures
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from typer.testing import CliRunner

from algodesk.api.market import (
    Bar,
    CoverageStatus,
    InstrumentId,
    SeriesKey,
)
from algodesk.api.screens import ScreenBars
from algodesk.cli.app import app
from algodesk.data.accumulate import plan_accumulation
from algodesk.data.coverage import repair_coverage
from algodesk.data.fetcher import Fetcher
from algodesk.data.manifest import (
    CoverageDayRecord,
    DataManifest,
    DatasetRecord,
    PartRecord,
)
from algodesk.data.schema import write_bars_parquet
from algodesk.data.store import BarStore
from algodesk.ops.runtime import open_runtime
from algodesk.ops.screens import DataServiceScreenAdapter
from algodesk.screens.models import TomlScreenModel


class MockCalendar:
    """Hermetic test calendar for testing."""

    def __init__(self) -> None:
        self.tz = ZoneInfo("Asia/Kolkata")

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        return d.weekday() < 5

    def sessions(self, day: date, segment: str = "") -> list[object]:
        if not self.is_trading_day(day):
            return []
        s_start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=self.tz).astimezone(UTC)
        s_end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=self.tz).astimezone(UTC)
        from algodesk.api.protocols import Session

        return [Session(name="regular", start=s_start, end=s_end, tradable=True)]


class MockJournal:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, kind: str, payload: dict[str, Any], durable: bool = True) -> int:
        self.events.append({"kind": kind, "payload": payload})
        return len(self.events)


def _make_bars_for_days(sym: str, days: list[date], timeframe: str = "1d") -> list[Bar]:
    ist = ZoneInfo("Asia/Kolkata")
    inst = InstrumentId(f"NSE:EQ:{sym}")
    bars = []
    for d in days:
        s_dt = datetime(d.year, d.month, d.day, 9, 15, tzinfo=ist).astimezone(UTC)
        e_dt = datetime(d.year, d.month, d.day, 15, 30, tzinfo=ist).astimezone(UTC)
        bars.append(
            Bar(
                instrument=inst,
                timeframe=timeframe,
                start_ts=s_dt,
                end_ts=e_dt,
                open=100.0,
                high=105.0,
                low=99.0,
                close=102.0,
                volume=1000,
                complete=True,
            )
        )
    return bars


def test_concurrent_commits_thread_safe(tmp_path: Path) -> None:
    """Committing 50 symbols across 4 threads completes with 0 errors and 0 mismatches."""
    data_dir = tmp_path / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    store = BarStore(data_dir=data_dir, manifest=manifest)

    days = [date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)]

    def commit_symbol(sym_idx: int) -> None:
        sym = f"SYM{sym_idx:03d}"
        key = SeriesKey(InstrumentId(f"NSE:EQ:{sym}"), "bars", "1d", "raw", "upstox")
        bars = _make_bars_for_days(sym, days)
        store.commit(key, bars)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(commit_symbol, i) for i in range(50)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    # Verify all 50 symbols have 3 bars in daily.parquet and 3 COMPLETE coverage rows
    for i in range(50):
        sym = f"SYM{i:03d}"
        ds_id = f"bars:upstox:1d:NSE:EQ:{sym}:raw"
        parts = manifest.get_parts(ds_id)
        assert len(parts) == 1
        assert parts[0].rows == 3

        cov = manifest.get_coverage_days(ds_id)
        assert len(cov) == 3
        for c in cov:
            assert c.status == CoverageStatus.COMPLETE
            assert c.rows == 1


def test_data_verify_detects_coverage_without_bars_and_bars_without_coverage(
    tmp_project: Path,
) -> None:
    """data verify detects coverage_without_bars and bars_without_coverage and fails cleanly."""
    runner = CliRunner()
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(m_path)

    # 1. Dataset with coverage marked COMPLETE but NO bar in parquet (coverage_without_bars)
    ds_cov_only = "bars:upstox:1d:NSE:EQ:COV_ONLY:raw"
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_cov_only,
            instrument="NSE:EQ:COV_ONLY",
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source="upstox",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_cov_only,
            day="2026-11-13",
            status=CoverageStatus.COMPLETE,
            rows=1,
            expected_rows=1,
            part_path="",
            fetched_at="2026-11-14T00:00:00Z",
            note="",
        )
    )

    # 2. Dataset with bar in parquet but coverage is MISSING (bars_without_coverage)
    ds_bar_only = "bars:upstox:1d:NSE:EQ:BAR_ONLY:raw"
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_bar_only,
            instrument="NSE:EQ:BAR_ONLY",
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source="upstox",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_bar_only,
            day="2026-11-13",
            status=CoverageStatus.MISSING,
            rows=0,
            expected_rows=1,
            part_path="",
            fetched_at="2026-11-14T00:00:00Z",
            note="",
        )
    )
    bars = _make_bars_for_days("BAR_ONLY", [date(2026, 11, 13)])
    p_path = (
        data_dir
        / "bars"
        / "source=upstox"
        / "timeframe=1d"
        / "exchange=NSE"
        / "segment=EQ"
        / "symbol=BAR_ONLY"
        / "adj=raw"
        / "daily.parquet"
    )
    write_bars_parquet(p_path, bars)
    from algodesk.cli.data import _compute_sha256

    manifest.record_part(
        PartRecord(
            path=str(p_path.relative_to(data_dir)),
            dataset_id=ds_bar_only,
            rows=1,
            bytes=p_path.stat().st_size,
            first_ts=bars[0].start_ts.isoformat(),
            last_ts=bars[0].start_ts.isoformat(),
            sha256=_compute_sha256(p_path),
            source="upstox",
            source_version="1.0",
            fetched_at="2026-11-14T00:00:00Z",
        )
    )

    # Run verify with --json
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--json"])
    assert res.exit_code != 0
    parsed = json.loads(res.stdout)
    assert parsed["ok"] is False
    assert any("COV_ONLY" in x and "2026-11-13" in x for x in parsed["coverage_without_bars"])
    assert any("BAR_ONLY" in x and "2026-11-13" in x for x in parsed["bars_without_coverage"])

    # Run verify in text mode
    res_text = runner.invoke(app, ["--root", str(tmp_project), "data", "verify"])
    assert res_text.exit_code != 0
    assert "VERIFY: FAILED" in res_text.stdout
    assert "Coverage without bars" in res_text.stdout
    assert "Bars without coverage" in res_text.stdout


def test_coverage_repair_dry_run_and_live(tmp_project: Path) -> None:
    """coverage repair sets COMPLETE days without stored bars to MISSING."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(m_path)
    journal = MockJournal()

    ds_id = "bars:upstox:1d:NSE:EQ:REPAIR_TEST:raw"
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:REPAIR_TEST",
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source="upstox",
        )
    )
    # Stored bar on 2026-11-11 only
    bars = _make_bars_for_days("REPAIR_TEST", [date(2026, 11, 11)])
    p_path = (
        data_dir
        / "bars"
        / "source=upstox"
        / "timeframe=1d"
        / "exchange=NSE"
        / "segment=EQ"
        / "symbol=REPAIR_TEST"
        / "adj=raw"
        / "daily.parquet"
    )
    write_bars_parquet(p_path, bars)
    from algodesk.cli.data import _compute_sha256

    manifest.record_part(
        PartRecord(
            path=str(p_path.relative_to(data_dir)),
            dataset_id=ds_id,
            rows=1,
            bytes=p_path.stat().st_size,
            first_ts=bars[0].start_ts.isoformat(),
            last_ts=bars[0].start_ts.isoformat(),
            sha256=_compute_sha256(p_path),
            source="upstox",
            source_version="1.0",
            fetched_at="2026-11-14T00:00:00Z",
        )
    )
    # Coverage records: 2026-11-11 and 2026-11-12 both COMPLETE
    for d_str in ("2026-11-11", "2026-11-12"):
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=d_str,
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path=str(p_path.relative_to(data_dir)),
                fetched_at="2026-11-14T00:00:00Z",
                note="",
            )
        )

    # 1. Dry run repair
    rep_dry = repair_coverage(
        manifest=manifest,
        data_dir=data_dir,
        source="upstox",
        timeframe="1d",
        dry_run=True,
        journal=journal,
    )
    assert len(rep_dry) == 1
    assert rep_dry[0].day == "2026-11-12"
    assert rep_dry[0].new_status == CoverageStatus.MISSING
    assert len(journal.events) == 0

    # Ensure status remained COMPLETE after dry-run
    cov_recs = {r.day: r for r in manifest.get_coverage_days(ds_id)}
    assert cov_recs["2026-11-12"].status == CoverageStatus.COMPLETE

    # 2. Live repair
    rep_live = repair_coverage(
        manifest=manifest,
        data_dir=data_dir,
        source="upstox",
        timeframe="1d",
        dry_run=False,
        journal=journal,
    )
    assert len(rep_live) == 1
    assert rep_live[0].day == "2026-11-12"
    assert rep_live[0].new_status == CoverageStatus.MISSING

    # Ensure status is now MISSING
    cov_recs_after = {r.day: r for r in manifest.get_coverage_days(ds_id)}
    assert cov_recs_after["2026-11-12"].status == CoverageStatus.MISSING
    assert cov_recs_after["2026-11-12"].rows == 0
    assert "Reset to MISSING" in cov_recs_after["2026-11-12"].note

    # Journal event logged with symbol_counts
    assert len(journal.events) == 1
    ev = journal.events[0]
    assert ev["kind"] == "data.coverage.repair"
    assert ev["payload"]["symbol_counts"]["REPAIR_TEST"] == 1


def test_screen_skip_message_coverage_bars_disagree(tmp_project: Path) -> None:
    """When coverage is COMPLETE but bar missing in window, skip message names coverage repair."""
    with open_runtime(tmp_project) as rt:
        manifest = rt.paths.data / "state" / "data_manifest.db"
        m = DataManifest(manifest)
        store = BarStore(rt.paths.data, m)

        sym = "NSE:EQ:DISAGREE_SYM"
        ds_id = f"bars:upstox:1d:{sym}:raw"
        m.record_dataset(
            DatasetRecord(
                dataset_id=ds_id,
                instrument=sym,
                kind="bars",
                timeframe="1d",
                adjustment="raw",
                source="upstox",
            )
        )
        # Stored bar only on 2026-11-11
        bars = _make_bars_for_days("DISAGREE_SYM", [date(2026, 11, 11)])
        store.commit(SeriesKey(InstrumentId(sym), "bars", "1d", "raw", "upstox"), bars)

        # But coverage claims 2026-11-12 is ALSO COMPLETE
        m.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day="2026-11-12",
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path="dummy.parquet",
                fetched_at="2026-11-14T00:00:00Z",
                note="",
            )
        )

        from algodesk.ops.screens import build_screen_runner
        from algodesk.screens.models import OperandModel, RequireRule

        runner = build_screen_runner(rt)
        screen = TomlScreenModel(
            name="test_screen",
            timeframe="1d",
            adjust="splits",
            require=[
                RequireRule(
                    left=OperandModel(field="close"),
                    op=">",
                    right=OperandModel(const=0.0),
                )
            ],
        )
        # Run screen as-of 2026-11-12
        res = runner.run(
            screen=screen,
            symbols=["DISAGREE_SYM"],
            as_of=date(2026, 11, 12),
        )
        print("RES ROWS:", res.rows)
        print("RES SKIPPED:", res.skipped)
        assert len(res.skipped) == 1
        sym_name, reason, detail = res.skipped[0]
        assert "coverage and bars disagree: run algodesk data coverage repair" in detail


def test_noop_accumulate_plan_fast_and_statement_bounds(tmp_project: Path) -> None:
    """A no-op accumulate plan for 500 up-to-date symbols executes <= 10s with 0 parquet opens."""
    import time

    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(m_path)
    cal = MockCalendar()

    # Create config/groups/test500.toml with 500 symbols
    grp_dir = tmp_project / "config" / "groups"
    grp_dir.mkdir(parents=True, exist_ok=True)
    sym_lines = "\n".join(f'  "NSE:EQ:SYM{i:03d}",' for i in range(500))
    (grp_dir / "test500.toml").write_text(
        f'name = "test500"\ndescription = "500 symbols"\nmembers = [\n{sym_lines}\n]\n'
    )

    # Generate 500 up-to-date symbols in manifest
    days = [date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)]
    for i in range(500):
        sym = f"SYM{i:03d}"
        ds_id = f"bars:upstox:1d:NSE:EQ:{sym}:raw"
        manifest.record_dataset(
            DatasetRecord(
                dataset_id=ds_id,
                instrument=f"NSE:EQ:{sym}",
                kind="bars",
                timeframe="1d",
                adjustment="raw",
                source="upstox",
                adjustment_basis="splits",
            )
        )
        for d in days:
            manifest.record_coverage_day(
                CoverageDayRecord(
                    dataset_id=ds_id,
                    day=d.isoformat(),
                    status=CoverageStatus.COMPLETE,
                    rows=1,
                    expected_rows=1,
                    part_path=f"bars/{sym}.parquet",
                    fetched_at="2026-11-14T00:00:00Z",
                    note="",
                )
            )

    # Track parquet file opens and SQL statements
    open_count = 0
    stmt_count = 0

    import pyarrow.parquet as pq_module

    orig_read_table = pq_module.read_table

    def tracking_read_table(*args: Any, **kwargs: Any) -> Any:
        nonlocal open_count
        open_count += 1
        return orig_read_table(*args, **kwargs)

    pq_module.read_table = tracking_read_table  # type: ignore[assignment]

    class StmtCountingConnection:
        def __init__(self, conn: Any) -> None:
            self._real = conn

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            nonlocal stmt_count
            stmt_count += 1
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

    manifest._conn = StmtCountingConnection(manifest._conn)  # type: ignore[assignment]

    with open_runtime(tmp_project) as rt:
        try:
            t0 = time.perf_counter()
            instruments, plan_items = plan_accumulation(
                group="test500",
                source="upstox",
                days=3,
                runtime=rt,
                calendar=cal,
                timeframe="1d",
                now_dt=datetime(2026, 11, 14, 0, 0, tzinfo=UTC),
            )
            elapsed = time.perf_counter() - t0
        finally:
            pq_module.read_table = orig_read_table  # type: ignore[assignment]

    assert elapsed <= 10.0, f"Plan took {elapsed:.2f}s > 10.0s"
    assert open_count == 0, f"Parquet opens: {open_count} > 0"
    assert stmt_count < 500, f"SQL statements: {stmt_count} >= 500"
    assert len(instruments) == 500
    assert len(plan_items) == 500
    # All symbols up to date: 0 fetchable
    assert all(len(item.fetchable_sessions) == 0 for item in plan_items)


def test_fetch_max_workers_default_is_1(tmp_project: Path) -> None:
    """data.fetch_max_workers defaults to 1 in config and fetcher fallback."""
    from algodesk.params.defaults import load_defaults

    specs = load_defaults(tmp_project / "config" / "params")
    by_name = {s.name: s for s in specs}
    assert "data.fetch_max_workers" in by_name
    assert by_name["data.fetch_max_workers"].default == 1

    # Fetcher without params falls back to 1
    store = BarStore(tmp_project / "data", None)  # type: ignore[arg-type]
    fetcher = Fetcher(store=store, limiter=None, sources={})  # type: ignore[arg-type]
    assert fetcher._workers == 1


def test_data_verify_passes_when_coverage_and_bars_agree(tmp_project: Path) -> None:
    """data verify passes cleanly when coverage and stored bars agree."""
    runner = CliRunner()
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(m_path)
    store = BarStore(data_dir, manifest)

    sym = "NSE:EQ:AGREE_SYM"
    key = SeriesKey(InstrumentId(sym), "bars", "1d", "raw", "upstox")
    bars = _make_bars_for_days("AGREE_SYM", [date(2026, 11, 11), date(2026, 11, 12)])
    store.commit(key, bars)

    res = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--json"])
    assert res.exit_code == 0
    parsed = json.loads(res.stdout)
    assert parsed["ok"] is True
    assert len(parsed["coverage_without_bars"]) == 0
    assert len(parsed["bars_without_coverage"]) == 0


def test_data_verify_only_ticks_skips_bar_coverage_checks(tmp_project: Path) -> None:
    """data verify --only ticks skips bar coverage discrepancy checks."""
    runner = CliRunner()
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(m_path)

    # Broken bar coverage record
    ds_id = "bars:upstox:1d:NSE:EQ:BROKEN:raw"
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument="NSE:EQ:BROKEN",
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source="upstox",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-13",
            status=CoverageStatus.COMPLETE,
            rows=1,
            expected_rows=1,
            part_path="",
            fetched_at="2026-11-14T00:00:00Z",
            note="",
        )
    )

    # --only ticks must ignore bar coverage discrepancies
    res = runner.invoke(
        app, ["--root", str(tmp_project), "data", "verify", "--only", "ticks", "--json"]
    )
    assert res.exit_code == 0
    parsed = json.loads(res.stdout)
    assert parsed["ok"] is True
    assert len(parsed["coverage_without_bars"]) == 0


def test_coverage_repair_filters_by_source_and_timeframe(tmp_project: Path) -> None:
    """coverage repair filters updates by source and timeframe."""
    data_dir = tmp_project / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(m_path)

    ds_upstox = "bars:upstox:1d:NSE:EQ:SYM_U:raw"
    ds_yfinance = "bars:yfinance:1d:NSE:EQ:SYM_Y:raw"

    manifest.record_dataset(DatasetRecord(ds_upstox, "NSE:EQ:SYM_U", "bars", "1d", "raw", "upstox"))
    manifest.record_dataset(
        DatasetRecord(ds_yfinance, "NSE:EQ:SYM_Y", "bars", "1d", "raw", "yfinance")
    )

    manifest.record_coverage_day(
        CoverageDayRecord(
            ds_upstox, "2026-11-12", CoverageStatus.COMPLETE, 1, 1, "", "2026-11-14T00:00:00Z", ""
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            ds_yfinance, "2026-11-12", CoverageStatus.COMPLETE, 1, 1, "", "2026-11-14T00:00:00Z", ""
        )
    )

    repaired = repair_coverage(
        manifest=manifest,
        data_dir=data_dir,
        source="upstox",
        timeframe="1d",
        dry_run=False,
    )
    assert len(repaired) == 1
    assert repaired[0].dataset_id == ds_upstox

    # yfinance remained COMPLETE
    y_recs = {r.day: r for r in manifest.get_coverage_days(ds_yfinance)}
    assert y_recs["2026-11-12"].status == CoverageStatus.COMPLETE


def test_screen_adapter_splits_maps_to_raw_and_falls_back(tmp_project: Path) -> None:
    """DataServiceScreenAdapter maps splits to raw, and falls back to adjusted if raw is empty."""
    with open_runtime(tmp_project) as rt:
        manifest = DataManifest(rt.paths.data / "state" / "data_manifest.db")
        store = BarStore(rt.paths.data, manifest)

        sym = "NSE:EQ:FALLBACK_SYM"
        # Store bars in adjusted partition only
        key_adj = SeriesKey(InstrumentId(sym), "bars", "1d", "adjusted", "upstox")
        bars = _make_bars_for_days("FALLBACK_SYM", [date(2026, 11, 11), date(2026, 11, 12)])
        store.commit(key_adj, bars)

        from algodesk.ops.screens import build_data_service

        ds = build_data_service(rt)
        adapter = DataServiceScreenAdapter(ds, default_source="upstox")
        assert adapter._ADJUST_TO_DOWNLOAD["splits"] == "raw"

        # Reading with adjust="splits" falls back to adjusted partition when raw is empty
        res = adapter.get_bars(
            instrument=sym,
            timeframe="1d",
            start=datetime(2026, 11, 11, 0, 0, tzinfo=UTC),
            end=datetime(2026, 11, 12, 23, 59, tzinfo=UTC),
            adjust="splits",
        )
        assert isinstance(res, ScreenBars)
        assert len(res.bars) == 2


def test_screen_skip_message_regular_gap_names_accumulate(tmp_project: Path) -> None:
    """When a gap exists in the window without coverage, skip message names data accumulate."""
    with open_runtime(tmp_project) as rt:
        manifest = DataManifest(rt.paths.data / "state" / "data_manifest.db")
        store = BarStore(rt.paths.data, manifest)

        sym = "NSE:EQ:REGULAR_GAP_SYM"
        ds_id = f"bars:upstox:1d:{sym}:raw"
        manifest.record_dataset(DatasetRecord(ds_id, sym, "bars", "1d", "raw", "upstox"))
        bars = _make_bars_for_days("REGULAR_GAP_SYM", [date(2026, 11, 9), date(2026, 11, 12)])
        store.commit(SeriesKey(InstrumentId(sym), "bars", "1d", "raw", "upstox"), bars)

        # Mark 2026-11-10 as MISSING in coverage
        manifest.record_coverage_day(
            CoverageDayRecord(
                ds_id, "2026-11-10", CoverageStatus.MISSING, 0, 1, "", "2026-11-14T00:00:00Z", ""
            )
        )

        from algodesk.ops.screens import build_screen_runner
        from algodesk.screens.models import OperandModel, RequireRule

        runner = build_screen_runner(rt)
        screen = TomlScreenModel(
            name="test_reg_gap",
            timeframe="1d",
            adjust="splits",
            require=[
                RequireRule(
                    left=OperandModel(field="close"),
                    op=">",
                    right=OperandModel(const=0.0),
                )
            ],
        )
        res = runner.run(
            screen=screen,
            symbols=["REGULAR_GAP_SYM"],
            as_of=date(2026, 11, 12),
        )
        assert len(res.skipped) == 1
        sym_name, reason, detail = res.skipped[0]
        assert "Fix with: algodesk data accumulate" in detail
        assert "coverage and bars disagree" not in detail


def test_coverage_repair_preserves_closed_days(tmp_project: Path) -> None:
    """repair_coverage does not turn a CLOSED day into MISSING even if no bars exist."""
    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "state" / "data_manifest.db")

    ds_id = "bars:upstox:1d:NSE:EQ:CLOSED_SYM:raw"
    manifest.record_dataset(
        DatasetRecord(ds_id, "NSE:EQ:CLOSED_SYM", "bars", "1d", "raw", "upstox")
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-11-08",
            status=CoverageStatus.CLOSED,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at="2026-11-14T00:00:00Z",
            note="Session closed",
        )
    )

    repaired = repair_coverage(
        manifest=manifest,
        data_dir=data_dir,
        source="upstox",
        timeframe="1d",
        dry_run=False,
    )
    # Closed day is preserved, not reset to MISSING
    assert len(repaired) == 0
    rec = manifest.get_coverage_days(ds_id)[0]
    assert rec.status == CoverageStatus.CLOSED
