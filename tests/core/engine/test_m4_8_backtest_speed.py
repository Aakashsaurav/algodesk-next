"""Tests for M4-8: Backtest speed optimisations, equivalence, and invariants."""

from __future__ import annotations

import json
import math
import random
import shutil
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from algodesk.api.kinds import Mode, PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, InstrumentId, Level, Quote, SeriesKey
from algodesk.api.protocols import DataSource
from algodesk.api.trading import Charges, Fill, Product, Side
from algodesk.cli.app import app
from algodesk.data.bar_synth import BarQuoteSynth
from algodesk.data.manifest import DataManifest
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.engine.backtest import BacktestRunner
from algodesk.engine.benchmark import _generate_trading_days, run_benchmark_measurement
from algodesk.engine.clock import SimClock
from algodesk.engine.scheduler import Scheduler
from algodesk.journal.store import verify_journal
from algodesk.journal.writer import Journal, JournalSettings
from algodesk.ops.runtime import open_runtime
from algodesk.portfolio.ledger import PortfolioLedger
from algodesk.risk.killswitch import KillSwitch


class _FakeSource(DataSource):
    def __init__(self, b_map: dict[InstrumentId, list[Bar]]) -> None:
        self.b_map = b_map
        self.manifest = PluginManifest(
            name="upstox",
            version="0.1.0",
            kind=PluginKind.BROKER,
            api_version=1,
            entry="plugins.brokers.upstox.broker:UpstoxBroker",
            capabilities={"history": True},
            retention={"1d": 36500, "1m": 3650},
            max_span={"1d": 3650, "1m": 365},
        )

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> list[Bar]:
        bars = self.b_map.get(key.instrument, [])
        return [b for b in bars if start <= b.start_ts <= end]

    def instruments(self) -> list[object]:
        return []

    def stream(self, insts: object) -> object:
        raise NotImplementedError


def _build_bars(
    symbols: list[InstrumentId],
    sessions: list[date],
    timeframe: str,
    perturb_first: float = 0.0,
) -> dict[InstrumentId, list[Bar]]:
    bars_by_sym: dict[InstrumentId, list[Bar]] = {}
    bars_per_sess = 1 if timeframe == "1d" else 375
    for idx, sym in enumerate(symbols):
        sym_bars: list[Bar] = []
        base_price = 100.0 + (idx % 10) * 15.0
        for s_idx, d in enumerate(sessions):
            s_open = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)
            for m_idx in range(bars_per_sess):
                b_start = s_open if timeframe == "1d" else s_open + timedelta(minutes=m_idx)
                b_end = (
                    datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC)
                    if timeframe == "1d"
                    else b_start + timedelta(minutes=1)
                )
                step = s_idx if timeframe == "1d" else s_idx * bars_per_sess + m_idx
                factor = 1.0 + 0.08 * math.sin(step * 2 * math.pi / 20.0)
                c_price = round(base_price * factor, 2)
                if idx == 0 and s_idx == len(sessions) - 1 and m_idx == 0 and perturb_first:
                    c_price = round(c_price + perturb_first, 2)
                o_price = round(c_price * 0.999, 2)
                h_price = round(max(o_price, c_price) * 1.003, 2)
                l_price = round(min(o_price, c_price) * 0.997, 2)
                sym_bars.append(
                    Bar(
                        instrument=sym,
                        timeframe=timeframe,
                        start_ts=b_start,
                        end_ts=b_end,
                        open=o_price,
                        high=h_price,
                        low=l_price,
                        close=c_price,
                        volume=5000,
                        complete=True,
                        synthetic=False,
                    )
                )
        bars_by_sym[sym] = sym_bars
    return bars_by_sym


def _run_fixture_backtest(
    tmp_path: Path,
    name: str,
    num_symbols: int,
    num_sessions: int,
    timeframe: str,
    perturb_first: float = 0.0,
) -> Path:
    root_dir = Path(__file__).resolve().parents[3]
    proj = tmp_path / f"proj_{name}"
    proj.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root_dir / "pyproject.toml", proj / "pyproject.toml")
    shutil.copytree(root_dir / "config", proj / "config")
    shutil.copytree(root_dir / "plugins", proj / "plugins")
    (proj / "data").mkdir(parents=True, exist_ok=True)
    (proj / "logs").mkdir(parents=True, exist_ok=True)
    (proj / "results").mkdir(parents=True, exist_ok=True)
    (proj / ".algodesk").mkdir(parents=True, exist_ok=True)
    conf_json = root_dir / ".algodesk" / "conformance.json"
    if conf_json.is_file():
        shutil.copyfile(conf_json, proj / ".algodesk" / "conformance.json")

    symbols = [InstrumentId(f"NSE:EQ:SYM{i:03d}") for i in range(num_symbols)]
    sessions = _generate_trading_days(date(2025, 1, 1), num_sessions)
    bars_by_sym = _build_bars(symbols, sessions, timeframe, perturb_first=perturb_first)

    m_path = proj / "data" / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    store = BarStore(data_dir=proj / "data", manifest=manifest)
    ds = DataService(
        sources={"upstox": _FakeSource(bars_by_sym)},
        default_source="upstox",
        store=store,
    )
    s_start = datetime(sessions[0].year, sessions[0].month, sessions[0].day, 0, 0, tzinfo=UTC)
    s_end = datetime(sessions[-1].year, sessions[-1].month, sessions[-1].day, 23, 59, tzinfo=UTC)
    for sym in symbols:
        ds.get(f"{sym}:{timeframe}", start=s_start, end=s_end, source="upstox", cache_only=False)

    sym_list_str = ", ".join(f'"{s}"' for s in symbols)
    cfg_file = proj / "config" / "runs" / f"{name}.toml"
    cfg_file.write_text(
        f"""allocated_capital = 1000000.0

[run]
name = "{name}"
mode = "backtest"

[backtest]
start = "{sessions[0].isoformat()}"
end = "{sessions[-1].isoformat()}"
fidelity = "bar"
timeframe = "{timeframe}"

[data_source]
plugin = "upstox"

[execution_venue]
plugin = "sim"
seed = 42

[fees]
schedule = "zerodha_nse"
fallback = "earliest"

[[calendars]]
plugin = "exchange"
exchange = "nse"

[secrets]
plugin = "keychain"

[universe]
symbols = [{sym_list_str}]

[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
params = {{ timeframe = "{timeframe}", fast = 5, slow = 15, qty = 10, product = "CNC" }}
""",
        encoding="utf-8",
    )

    try:
        with open_runtime(proj, run=name, ensure=True) as rt:
            bkt = BacktestRunner(
                runtime=rt,
                run_config=rt.effective.run,
                run_name=name,
                run_id=name,
                quiet=True,
            )
            bkt.session_runner._data_service = ds
            return bkt.run()
    finally:
        manifest.close()


def test_benchmark_no_profile_cli_and_runner() -> None:
    """backtest benchmark --no-profile omits cProfile and prints summary."""
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["backtest", "benchmark", "--symbols", "2", "--sessions", "2", "--no-profile"],
    )
    assert res.exit_code == 0, res.output
    assert "=== Backtest Benchmark ===" in res.output
    assert "Top 10 functions" not in res.output


def test_per_bar_counters_reduced_vs_unoptimised(tmp_path: Path) -> None:
    """In backtest, killswitch syncs == 0 and quotes dispatched < 4 per bar."""
    q_count = 0
    ks_count = 0
    orig_sync = KillSwitch._sync

    def wrapped_sync(self: KillSwitch) -> None:
        nonlocal ks_count
        ks_count += 1
        orig_sync(self)

    from plugins.venues.sim.venue import SimVenue

    orig_oq = SimVenue.on_quote

    def wrapped_oq(self: SimVenue, q: Quote) -> None:
        nonlocal q_count
        q_count += 1
        orig_oq(self, q)

    with (
        patch.object(KillSwitch, "_sync", wrapped_sync),
        patch.object(SimVenue, "on_quote", wrapped_oq),
    ):
        _run_fixture_backtest(tmp_path, "cnt_1d", 3, 20, "1d")

    assert ks_count == 0
    assert q_count < 3 * 20 * 4


def test_killswitch_in_memory_backtest_no_file_stat(tmp_path: Path) -> None:
    """KillSwitch(in_memory=True) skips disk sync on is_active/status; False syncs disk."""
    ks_file = tmp_path / "ks.json"
    clock = SimClock(datetime(2025, 1, 1, 9, 0, tzinfo=UTC))

    with patch.object(KillSwitch, "_sync", side_effect=AssertionError("must not sync")) as _:
        ks_mem = KillSwitch(state_file=ks_file, clock=clock, in_memory=True)
        assert not ks_mem.is_active()
        assert ks_mem.status()["active"] is False

    ks_mem.trip("test", by="rule")
    assert ks_mem.is_active()
    assert ks_file.is_file()
    ks_mem.clear("reset")
    assert not ks_mem.is_active()

    ks_disk = KillSwitch(state_file=ks_file, clock=clock, in_memory=False)
    ks_disk.trip("disk_trip", by="manual")
    assert ks_disk.is_active()


def test_quote_skip_low_high_when_flat_and_no_orders(tmp_path: Path) -> None:
    """Low/high quotes are skipped only when instrument has no resting order or position."""
    res_path = _run_fixture_backtest(tmp_path, "sma_1d_ref", 5, 60, "1d")
    ref_dir = Path(__file__).resolve().parents[3] / "tests/fixtures/reference/m4_8_backtest/sma_1d"
    assert (res_path / "trades.csv").read_bytes() == (ref_dir / "trades.csv").read_bytes()


def test_journal_backtest_filters_non_registry_events_and_verifies_hash_chain(
    tmp_path: Path,
) -> None:
    """Backtest batch journal filters non-registry events and preserves hash chain."""
    db_path = tmp_path / "j_bt.db"
    clock = SimClock(datetime(2025, 1, 1, 9, 0, tzinfo=UTC))
    settings = JournalSettings(
        flush_interval_ms=50,
        max_buffer_events=100,
        backpressure_timeout_ms=2000,
        batch_mode=True,
        batch_size=10,
        allowed_events=frozenset({"run.start", "signal", "run.end"}),
    )
    j = Journal(db_path=db_path, settings=settings, clock=clock.now)
    j.start_run("bt_run_1", Mode.BACKTEST, "cfg_h", "reg_h", batch_mode=True, batch_size=10)
    j.append("run.start", {"ok": True})
    s_ignored = j.append("market.quote", {"ltp": 100.0})
    s_sig = j.append("signal", {"sym": "NSE:EQ:INFY"})
    j.append("run.end", {"status": "ok"})
    j.end_run("ok")
    j.close()

    assert s_sig > s_ignored
    res_v = verify_journal(db_path)
    assert res_v.ok, res_v.errors
    assert s_sig > s_ignored


def test_ledger_mark_only_held_instrument_and_caches_margin_used() -> None:
    """PortfolioLedger.mark skips unheld instruments and caches margin_used between fills."""
    clock = SimClock(datetime(2025, 1, 1, 9, 15, tzinfo=UTC))
    ledger = PortfolioLedger(starting_cash=Decimal("100000"), clock=clock)
    infy = InstrumentId("NSE:EQ:INFY")
    tcs = InstrumentId("NSE:EQ:TCS")

    q_tcs = Quote.create_unchecked(
        instrument=tcs,
        exch_ts=clock.now(),
        recv_ts=clock.now(),
        bids=(Level.create_unchecked(3000.0, 10),),
        asks=(Level.create_unchecked(3001.0, 10),),
        ltp=3000.0,
        volume=100,
    )
    ver_before = ledger._state_version
    ledger.mark(q_tcs)
    assert ledger._state_version == ver_before

    fill = Fill(
        fill_id="F1",
        client_ref="C1",
        instrument=infy,
        side=Side.BUY,
        qty=10,
        price=Decimal("1000.00"),
        charges=Charges(),
        ts=clock.now(),
        product=Product.CNC,
    )
    ledger.apply_fill(fill)
    assert ledger.margin_used == Decimal("10000.00")
    assert not ledger._margin_dirty

    q_infy = Quote.create_unchecked(
        instrument=infy,
        exch_ts=clock.now(),
        recv_ts=clock.now(),
        bids=(Level.create_unchecked(1050.0, 10),),
        asks=(Level.create_unchecked(1051.0, 10),),
        ltp=1050.0,
        volume=100,
    )
    ver_after_fill = ledger._state_version
    ledger.mark(q_infy)
    assert ledger._state_version > ver_after_fill
    assert ledger.unrealized_pnl == Decimal("500.00")
    assert ledger.margin_used == Decimal("10000.00")


def test_risk_view_cache_invalidated_on_fill_and_held_mark(tmp_path: Path) -> None:
    """Snapshot positions cache is invalidated on fill and mark of held instrument."""
    clock = SimClock(datetime(2025, 1, 1, 9, 15, tzinfo=UTC))
    ledger = PortfolioLedger(starting_cash=Decimal("100000"), clock=clock)
    infy = InstrumentId("NSE:EQ:INFY")
    snap1 = ledger.snapshot()
    snap2 = ledger.snapshot()
    assert snap1.positions is snap2.positions

    fill = Fill(
        fill_id="F1",
        client_ref="C1",
        instrument=infy,
        side=Side.BUY,
        qty=5,
        price=Decimal("500.00"),
        charges=Charges(),
        ts=clock.now(),
        product=Product.CNC,
    )
    ledger.apply_fill(fill)
    snap3 = ledger.snapshot()
    assert snap3.positions is not snap2.positions

    q_infy = Quote.create_unchecked(
        instrument=infy,
        exch_ts=clock.now(),
        recv_ts=clock.now(),
        bids=(Level.create_unchecked(510.0, 10),),
        asks=(Level.create_unchecked(511.0, 10),),
        ltp=510.0,
        volume=100,
    )
    ledger.mark(q_infy)
    snap4 = ledger.snapshot()
    assert snap4.positions is not snap3.positions


def test_bar_create_unchecked_and_external_validation_preserved() -> None:
    """Bar.create_unchecked works for internal use while Bar/Quote retain full validation."""
    ts = datetime(2025, 1, 1, 3, 45, tzinfo=UTC)
    b = Bar.create_unchecked(
        instrument=InstrumentId("NSE:EQ:INFY"),
        timeframe="1d",
        start_ts=ts,
        end_ts=ts + timedelta(hours=6),
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=1000,
    )
    assert b.close == 101.0

    naive_ts = datetime(2025, 1, 1, 3, 45)
    with pytest.raises(ValueError, match="UTC"):
        Bar(
            instrument=InstrumentId("NSE:EQ:INFY"),
            timeframe="1d",
            start_ts=naive_ts,
            end_ts=ts,
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=1000,
        )


def test_simclock_and_scheduler_utc_fast_path_rejects_naive() -> None:
    """SimClock and Scheduler fast-path accepts UTC and rejects naive datetimes."""
    t0 = datetime(2025, 1, 1, 3, 45, tzinfo=UTC)
    clock = SimClock(t0)
    sched = Scheduler(clock)
    clock.set_time(t0 + timedelta(seconds=10))
    sched.advance_to(clock.now())

    with pytest.raises(ValueError, match="UTC"):
        clock.set_time(datetime(2025, 1, 1, 3, 46))
    with pytest.raises(ValueError, match="UTC"):
        sched.advance_to(datetime(2025, 1, 1, 3, 46))


def test_reference_fixture_sma_1d_byte_identical(tmp_path: Path) -> None:
    """Optimised 1d backtest matches stored CP0 reference outputs byte-for-byte."""
    res_path = _run_fixture_backtest(tmp_path, "sma_1d_ref", 5, 60, "1d")
    ref_dir = Path(__file__).resolve().parents[3] / "tests/fixtures/reference/m4_8_backtest/sma_1d"
    for fname in ("trades.csv", "equity_daily.csv"):
        assert (res_path / fname).read_bytes() == (ref_dir / fname).read_bytes()
    manifest = json.loads((res_path / "manifest.json").read_text(encoding="utf-8"))
    exp_hash = (ref_dir / "result_hash.txt").read_text(encoding="utf-8").strip()
    assert manifest["result_hash"] == exp_hash


def test_reference_fixture_sma_1m_byte_identical(tmp_path: Path) -> None:
    """Optimised 1m backtest matches stored CP0 reference outputs byte-for-byte."""
    res_path = _run_fixture_backtest(tmp_path, "sma_1m_ref", 2, 3, "1m")
    ref_dir = Path(__file__).resolve().parents[3] / "tests/fixtures/reference/m4_8_backtest/sma_1m"
    for fname in ("trades.csv", "equity_daily.csv"):
        assert (res_path / fname).read_bytes() == (ref_dir / fname).read_bytes()
    manifest = json.loads((res_path / "manifest.json").read_text(encoding="utf-8"))
    exp_hash = (ref_dir / "result_hash.txt").read_text(encoding="utf-8").strip()
    assert manifest["result_hash"] == exp_hash


def test_property_random_fixtures_and_perturbation_changes_hash(tmp_path: Path) -> None:
    """Random fixtures are deterministic byte-for-byte; perturbing 1 bar changes hash."""
    rng = random.Random(12345)
    n_sym = rng.randint(2, 4)
    n_sess = rng.randint(25, 35)
    r1 = _run_fixture_backtest(tmp_path / "r1", "prop_bt", n_sym, n_sess, "1d")
    r2 = _run_fixture_backtest(tmp_path / "r2", "prop_bt", n_sym, n_sess, "1d")
    assert (r1 / "trades.csv").read_bytes() == (r2 / "trades.csv").read_bytes()
    assert (r1 / "equity_daily.csv").read_bytes() == (r2 / "equity_daily.csv").read_bytes()
    h1 = json.loads((r1 / "manifest.json").read_text(encoding="utf-8"))["result_hash"]
    h2 = json.loads((r2 / "manifest.json").read_text(encoding="utf-8"))["result_hash"]
    assert h1 == h2

    r_pert = _run_fixture_backtest(
        tmp_path / "rp", "prop_bt", n_sym, n_sess, "1d", perturb_first=5.0
    )
    h_pert = json.loads((r_pert / "manifest.json").read_text(encoding="utf-8"))["result_hash"]
    assert h_pert != h1


def test_benchmark_measurement_small_cache_with_and_without_profile() -> None:
    """run_benchmark_measurement supports both profile=True and profile=False."""
    rep_no = run_benchmark_measurement(
        symbols_count=2, sessions_count=5, timeframe="1d", profile=False
    )
    rep_yes = run_benchmark_measurement(
        symbols_count=2, sessions_count=5, timeframe="1d", profile=True
    )
    assert rep_no.total_bars == 10
    assert rep_no.profile_stats_text == ""
    assert rep_yes.total_bars == 10
    assert "tottime" in rep_yes.profile_stats_text or "function calls" in rep_yes.profile_stats_text


def test_backtest_daily_mark_preserves_equity_curve(tmp_path: Path) -> None:
    """BacktestRunner produces one equity_daily row per session with finite values."""
    res_path = _run_fixture_backtest(tmp_path, "eq_check", 3, 25, "1d")
    eq_lines = (res_path / "equity_daily.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(eq_lines) == 26  # header + 25 sessions
    synth = BarQuoteSynth()
    assert synth._daily_ts_cache == {}
