"""Unit and integration tests for M3-6:

- Cache-only screen reader: never calls DataSource or network; skips with honest fix message
- Newly listed symbols and insufficient warm-up skipped as insufficient_history
- Screen runner always writes results bundle on skip failure, exception, interrupt, BrokenPipeError
- Progress lines go to stderr
- 1d chunking uses declared plugin max_span (500 days = 1 chunk; chunks of 3650 days)
- data.fetch_chunk_days applies only where source span is short (1m)
- RateLimiter concurrency across 4 workers on fake clock respects 1s, 1m, and 30m windows
"""

from __future__ import annotations

import io
import threading
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, Instrument, InstrumentId, SeriesKey
from algodesk.api.protocols import Calendar, Clock, DataSource
from algodesk.api.screens import (
    ScreenRefusal,
    SkipReason,
)
from algodesk.data.limiter import RateLimiter
from algodesk.data.manifest import DataManifest
from algodesk.data.planner import GapPlanner
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.ops.screens import DataServiceScreenAdapter, PluginIndicatorResolver
from algodesk.results.screen_writer import write_screen_results
from algodesk.screens.models import OperandModel, RequireRule, TomlScreenModel
from algodesk.screens.runner import ScreenRunner

IST = ZoneInfo("Asia/Kolkata")


class FakeCalendar(Calendar):
    """Hermetic test calendar covering 2024 to 2027."""

    def coverage(self) -> tuple[date, date]:
        return date(2024, 1, 1), date(2027, 12, 31)

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        return d.weekday() < 5  # Mon-Fri

    def sessions(self, d: date, segment: str = "") -> list[Any]:
        return []

    def next_session(self, ts: datetime, segment: str = "") -> Any:
        return None


class RaisingDataSource(DataSource):
    """DataSource whose every method raises to verify cache-only behavior."""

    def __init__(self) -> None:
        self.call_count = 0

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Sequence[Bar]:
        self.call_count += 1
        raise RuntimeError("Network fetch attempted on cache-only screen reader!")

    def instruments(self) -> Sequence[Instrument]:
        self.call_count += 1
        raise RuntimeError("Network fetch attempted on cache-only screen reader!")


def _generate_bars(
    instrument: str,
    prices: Sequence[float],
    as_of: date = date(2026, 11, 12),
) -> list[Bar]:
    inst_id = InstrumentId(instrument if ":" in instrument else f"NSE:EQ:{instrument}")
    n = len(prices)
    trading_days: list[date] = []
    curr = as_of
    while len(trading_days) < n:
        if curr.weekday() < 5:
            trading_days.append(curr)
        curr -= timedelta(days=1)
    trading_days.reverse()

    bars: list[Bar] = []
    for d, p in zip(trading_days, prices, strict=True):
        s_dt = datetime.combine(d, time(9, 15), tzinfo=IST).astimezone(UTC)
        e_dt = datetime.combine(d, time(15, 30), tzinfo=IST).astimezone(UTC)
        bars.append(
            Bar(
                instrument=inst_id,
                timeframe="1d",
                start_ts=s_dt,
                end_ts=e_dt,
                open=float(p),
                high=float(p) * 1.01,
                low=float(p) * 0.99,
                close=float(p),
                volume=1000,
                complete=True,
            )
        )
    return bars


def _build_test_env(
    tmp_path: Path, source_name: str = "upstox"
) -> tuple[DataService, BarStore, RaisingDataSource]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "data_manifest.db")
    cal = FakeCalendar()
    store = BarStore(data_dir, manifest, calendar=cal)
    raising_source = RaisingDataSource()

    manifest_obj = PluginManifest.model_validate(
        {
            "name": source_name,
            "kind": "broker",
            "version": "1.0.0",
            "api_version": 1,
            "entry": f"broker:{source_name}",
            "capabilities": {"candles": True, "history": True},
            "candles_adjustment_basis": "splits",
            "max_span": {"1d": 3650, "1m": 30},
            "limits": {
                "history": {
                    "requests_per_second": 50,
                    "requests_per_minute": 500,
                    "requests_per_30min": 2000,
                }
            },
        }
    )

    ds = DataService(
        sources={source_name: raising_source},
        default_source=source_name,
        store=store,
        manifests={source_name: manifest_obj},
        calendar=cal,
    )
    return ds, store, raising_source


# =========================================================================
# Step 5: Cache-only screen reader tests
# =========================================================================


def test_screen_reader_never_calls_datasource(tmp_path: Path) -> None:
    """Screen execution with RaisingDataSource evaluates cached data without network calls."""
    ds, store, raising_source = _build_test_env(tmp_path)
    as_of = date(2026, 11, 12)

    # Commit 60 bars for SYM_CACHED
    sym_ok = "NSE:EQ:SYM_CACHED"
    bars = _generate_bars(sym_ok, [120.0] * 60, as_of=as_of)
    key = SeriesKey(InstrumentId(sym_ok), "bars", "1d", "adjusted", "upstox")
    store.commit(key, bars)

    adapter = DataServiceScreenAdapter(ds, default_source="upstox")
    resolver = PluginIndicatorResolver()
    runner = ScreenRunner(adapter, resolver)

    screen = TomlScreenModel(
        name="cache_screen",
        timeframe="1d",
        adjust="splits",
        require=[
            RequireRule(
                left=OperandModel(ind="sma", n=50),
                op=">",
                right=OperandModel(const=100.0),
            )
        ],
    )

    result = runner.run(
        screen, symbols=[sym_ok, "NSE:EQ:SYM_MISSING"], as_of=as_of, group="nifty500"
    )

    assert raising_source.call_count == 0
    passed = [r for r in result.rows if r.passed]
    assert len(passed) == 1
    assert passed[0].symbol == sym_ok

    # Missing symbol skipped with NO_DATA and fix message
    skipped_missing = [s for s in result.skipped if "SYM_MISSING" in s[0]]
    assert len(skipped_missing) == 1
    assert skipped_missing[0][1] == SkipReason.NO_DATA
    fix_msg = "Fix with: algodesk data accumulate --group nifty500 --timeframe 1d"
    assert fix_msg in skipped_missing[0][2]


def test_newly_listed_and_insufficient_history_skipped(tmp_path: Path) -> None:
    """Symbols with fewer bars than warm-up or newly listed are skipped as insufficient_history."""
    ds, store, _ = _build_test_env(tmp_path)
    as_of = date(2026, 11, 12)

    # 10 bars when indicator needs 50 bars
    sym_few = "NSE:EQ:SYM_NEWLY_LISTED"
    bars = _generate_bars(sym_few, [150.0] * 10, as_of=as_of)
    key = SeriesKey(InstrumentId(sym_few), "bars", "1d", "adjusted", "upstox")
    store.commit(key, bars)

    adapter = DataServiceScreenAdapter(ds, default_source="upstox")
    resolver = PluginIndicatorResolver()
    runner = ScreenRunner(adapter, resolver, max_skipped_pct=100.0)

    screen = TomlScreenModel(
        name="warmup_screen",
        timeframe="1d",
        adjust="splits",
        require=[
            RequireRule(
                left=OperandModel(ind="sma", n=50),
                op=">",
                right=OperandModel(const=100.0),
            )
        ],
    )

    result = runner.run(screen, symbols=[sym_few], as_of=as_of, group="demo5")
    assert len(result.rows) == 0
    assert len(result.skipped) == 1
    sym, reason, detail = result.skipped[0]
    assert reason == SkipReason.INSUFFICIENT_HISTORY
    assert "Fix with: algodesk data accumulate --group demo5 --timeframe 1d" in detail


# =========================================================================
# Step 6: Clean result writing on error, interrupt, and BrokenPipeError
# =========================================================================


def test_progress_lines_directed_to_stderr(tmp_path: Path) -> None:
    """Screen progress lines are written to stderr, not stdout."""
    ds, store, _ = _build_test_env(tmp_path)
    as_of = date(2026, 11, 12)

    syms = [f"NSE:EQ:SYM_{i:02d}" for i in range(30)]
    for s in syms:
        bars = _generate_bars(s, [100.0] * 20, as_of=as_of)
        store.commit(SeriesKey(InstrumentId(s), "bars", "1d", "adjusted", "upstox"), bars)

    adapter = DataServiceScreenAdapter(ds, default_source="upstox")
    resolver = PluginIndicatorResolver()
    runner = ScreenRunner(adapter, resolver, progress_every=25)

    rule = RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=10.0))
    screen = TomlScreenModel(name="test_screen", timeframe="1d", require=[rule])

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        runner.run(screen, symbols=syms, as_of=as_of)

    # Progress line must be in stderr, not stdout
    assert "[screen] 25/30 symbols evaluated" in stderr_buf.getvalue()
    assert "[screen] 30/30 symbols evaluated" in stderr_buf.getvalue()
    assert "[screen]" not in stdout_buf.getvalue()


def test_result_folder_always_written_on_skip_guard_failure(tmp_path: Path) -> None:
    """When skip guard fails, verdict is FAIL and results folder is written."""
    adapter = MagicMock()
    adapter.get_bars.return_value = ScreenRefusal(reason=SkipReason.NO_DATA, detail="No bars found")
    runner = ScreenRunner(adapter, MagicMock(), max_skipped_pct=10.0)
    rule = RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=10.0))
    screen = TomlScreenModel(name="guard_fail_screen", timeframe="1d", require=[rule])

    res = runner.run(screen, symbols=["A", "B", "C"], as_of=date(2026, 11, 12))
    assert res.verdict == "FAIL"
    assert "exceeds maximum allowed screen.max_skipped_pct" in (res.fail_reason or "")

    results_dir = tmp_path / "results"
    run_dir = write_screen_results(results_dir, res)
    assert run_dir.is_dir()
    summary = (run_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Verdict: FAIL." in summary
    assert "exceeds maximum allowed screen.max_skipped_pct" in summary
    manifest_data = (run_dir / "manifest.json").read_text(encoding="utf-8")
    assert '"verdict": "FAIL"' in manifest_data


def test_result_folder_always_written_on_keyboard_interrupt(tmp_path: Path) -> None:
    """When KeyboardInterrupt occurs, create_aborted_result writes STOPPED verdict."""
    runner = ScreenRunner(MagicMock(), MagicMock())
    rule = RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=10.0))
    screen = TomlScreenModel(name="stopped_screen", timeframe="1d", require=[rule])

    res = runner.create_aborted_result(
        screen=screen,
        symbols=["SYM1", "SYM2"],
        as_of=date(2026, 11, 12),
        verdict="STOPPED",
        fail_reason="Interrupted by user (KeyboardInterrupt)",
    )
    assert res.verdict == "STOPPED"
    assert res.fail_reason == "Interrupted by user (KeyboardInterrupt)"

    results_dir = tmp_path / "results"
    run_dir = write_screen_results(results_dir, res)
    assert run_dir.is_dir()
    summary = (run_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Verdict: STOPPED." in summary
    assert "Interrupted by user (KeyboardInterrupt)" in summary
    manifest_data = (run_dir / "manifest.json").read_text(encoding="utf-8")
    assert '"verdict": "STOPPED"' in manifest_data


def test_result_folder_always_written_on_exception(tmp_path: Path) -> None:
    """When an unhandled exception occurs, create_aborted_result writes FAIL verdict."""
    runner = ScreenRunner(MagicMock(), MagicMock())
    rule = RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=10.0))
    screen = TomlScreenModel(name="failed_screen", timeframe="1d", require=[rule])

    res = runner.create_aborted_result(
        screen=screen,
        symbols=["SYM1"],
        as_of=date(2026, 11, 12),
        verdict="FAIL",
        fail_reason="Unexpected calculation error: division by zero",
    )
    assert res.verdict == "FAIL"

    results_dir = tmp_path / "results"
    run_dir = write_screen_results(results_dir, res)
    assert run_dir.is_dir()
    summary = (run_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Verdict: FAIL." in summary
    assert "Unexpected calculation error" in summary


def test_broken_pipe_suppresses_progress_and_finishes_run(tmp_path: Path) -> None:
    """When progress print hits BrokenPipeError, evaluation completes quietly."""
    ds, store, _ = _build_test_env(tmp_path)
    as_of = date(2026, 11, 12)

    syms = [f"NSE:EQ:SYM_{i:02d}" for i in range(30)]
    for s in syms:
        bars = _generate_bars(s, [100.0] * 20, as_of=as_of)
        store.commit(SeriesKey(InstrumentId(s), "bars", "1d", "adjusted", "upstox"), bars)

    adapter = DataServiceScreenAdapter(ds, default_source="upstox")
    resolver = PluginIndicatorResolver()
    runner = ScreenRunner(adapter, resolver, progress_every=10)

    rule = RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=10.0))
    screen = TomlScreenModel(name="pipe_screen", timeframe="1d", require=[rule])

    class BrokenPipeWriter:
        def write(self, s: str) -> int:
            if "[screen]" in s:
                raise BrokenPipeError(32, "Broken pipe")
            return len(s)

        def flush(self) -> None:
            pass

    with redirect_stderr(BrokenPipeWriter()):  # type: ignore[arg-type]
        res = runner.run(screen, symbols=syms, as_of=as_of)

    # Evaluation completed despite BrokenPipeError
    assert res.verdict == "PASS"
    assert len(res.rows) == 30


# =========================================================================
# Step 7: Chunking with plugin max_span tests
# =========================================================================


def test_1d_chunking_500_days_is_one_request() -> None:
    """A 500-day 1d series with Upstox max_span 3650 creates exactly one request chunk."""
    manifest_obj = PluginManifest.model_validate(
        {
            "name": "upstox",
            "kind": "broker",
            "version": "1.0.0",
            "api_version": 1,
            "entry": "broker:upstox",
            "capabilities": {"history": True},
            "max_span": {"1d": 3650, "1m": 30},
            "limits": {"max_span": {"1d": 3650, "1m": 30}},
        }
    )
    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "adjusted", "upstox")
    end_dt = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
    start_dt = end_dt - timedelta(days=500)

    plan = GapPlanner.plan(
        key=key,
        start=start_dt,
        end=end_dt,
        source_manifest=manifest_obj,
        chunk_days=30,  # data.fetch_chunk_days should NOT shrink 1d series
    )
    assert plan.has_work
    assert len(plan.chunks) == 1
    assert (plan.chunks[0].end_day - plan.chunks[0].start_day).days >= 490


def test_1d_chunking_chunks_period_into_3650_days() -> None:
    """A 1d series spanning 5000 days is chunked into 3650-day chunks."""
    manifest_obj = PluginManifest.model_validate(
        {
            "name": "upstox",
            "kind": "broker",
            "version": "1.0.0",
            "api_version": 1,
            "entry": "broker:upstox",
            "capabilities": {"history": True},
            "max_span": {"1d": 3650},
        }
    )
    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "adjusted", "upstox")
    end_dt = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
    start_dt = end_dt - timedelta(days=5000)

    plan = GapPlanner.plan(
        key=key,
        start=start_dt,
        end=end_dt,
        source_manifest=manifest_obj,
    )
    assert len(plan.chunks) == 2
    assert (plan.chunks[0].end_day - plan.chunks[0].start_day).days <= 3650


def test_chunking_with_fake_plugin_span() -> None:
    """Chunking uses fake plugin declared span (e.g. 200 days)."""
    fake_manifest = PluginManifest.model_validate(
        {
            "name": "fake_broker",
            "kind": "broker",
            "version": "1.0.0",
            "api_version": 1,
            "entry": "broker:fake",
            "capabilities": {"history": True},
            "max_span": {"1d": 200, "1m": 15},
        }
    )
    key = SeriesKey(InstrumentId("NSE:EQ:INFY"), "bars", "1d", "adjusted", "fake_broker")
    end_dt = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
    start_dt = end_dt - timedelta(days=500)

    plan = GapPlanner.plan(key=key, start=start_dt, end=end_dt, source_manifest=fake_manifest)
    assert len(plan.chunks) == 3
    for c in plan.chunks:
        assert (c.end_day - c.start_day).days + 1 <= 200


def test_1m_chunking_applies_fetch_chunk_days() -> None:
    """Short timeframe (1m) chunking applies data.fetch_chunk_days."""
    manifest_obj = PluginManifest.model_validate(
        {
            "name": "upstox",
            "kind": "broker",
            "version": "1.0.0",
            "api_version": 1,
            "entry": "broker:upstox",
            "capabilities": {"history": True},
            "max_span": {"1m": 30, "1d": 3650},
        }
    )
    key = SeriesKey(InstrumentId("NSE:EQ:TCS"), "bars", "1m", "raw", "upstox")
    end_dt = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
    start_dt = end_dt - timedelta(days=25)

    plan = GapPlanner.plan(
        key=key,
        start=start_dt,
        end=end_dt,
        source_manifest=manifest_obj,
        chunk_days=10,
    )
    assert len(plan.chunks) == 3
    for c in plan.chunks:
        assert (c.end_day - c.start_day).days + 1 <= 10


# =========================================================================
# Step 8: RateLimiter concurrency tests on fake clock
# =========================================================================


class FakeAdvancingClock(Clock):
    """Thread-safe fake clock advancing monotonically."""

    def __init__(self, start_ns: int = 1_000_000_000_000) -> None:
        self._ns = start_ns
        self._lock = threading.Lock()

    def monotonic_ns(self) -> int:
        with self._lock:
            return self._ns

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._ns += int(seconds * 1e9)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._ns / 1e9, tz=UTC)


def test_ratelimiter_resolves_registry_defaults() -> None:
    """RateLimiter resolves source.upstox.rate_limit_per_minute = 300."""
    params = MagicMock()
    params.get.side_effect = lambda k: {
        "source.upstox.rate_limit_per_minute": 300.0,
        "source.upstox.history.requests_per_second": 50.0,
        "source.upstox.history.requests_per_30min": 2000.0,
    }.get(k)

    limiter = RateLimiter(params=params)
    lim_s, lim_m, lim_30m = limiter.get_limits("upstox", "history")
    assert lim_s == 50
    assert lim_m == 300
    assert lim_30m == 2000


def test_ratelimiter_multi_worker_never_exceeds_windows() -> None:
    """With 4 concurrent workers on fake clock, no rate limit window is ever exceeded."""
    clock = FakeAdvancingClock()
    lock = threading.Lock()
    acquired_times: list[float] = []

    def fake_sleep(secs: float) -> None:
        with lock:
            clock.advance(max(secs, 0.005))

    params = MagicMock()
    # Tight limits for testing: 10 per sec, 30 per min, 100 per 30m
    params.get.side_effect = lambda k: {
        "source.test.history.requests_per_second": 10.0,
        "source.test.rate_limit_per_minute": 30.0,
        "source.test.history.requests_per_30min": 100.0,
    }.get(k)

    limiter = RateLimiter(params=params, clock=clock, sleep_fn=fake_sleep)

    def worker_task() -> None:
        for _ in range(15):
            limiter.acquire("test", "history")
            t_now = clock.monotonic_ns() / 1e9
            with lock:
                acquired_times.append(t_now)
                clock.advance(0.01)

    threads = [threading.Thread(target=worker_task) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(acquired_times) == 60
    acquired_times.sort()

    # Verify no 1.0s window exceeds 10 requests
    for t in acquired_times:
        window_1s = [x for x in acquired_times if t <= x < t + 1.0]
        assert len(window_1s) <= 10, f"Exceeded 1s limit at t={t}: count={len(window_1s)}"

    # Verify no 60.0s window exceeds 30 requests
    for t in acquired_times:
        window_60s = [x for x in acquired_times if t <= x < t + 60.0]
        assert len(window_60s) <= 30, f"Exceeded 60s limit at t={t}: count={len(window_60s)}"
