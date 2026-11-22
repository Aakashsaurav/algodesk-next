"""Tests for M4-5c: Backtest Kill-Switch Isolation, Continue Policy, and Safety Properties."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from algodesk.api import (
    Instrument,
    InstrumentId,
    Mode,
    OrderSpec,
    OrderType,
    PlaceOrder,
    Position,
    Product,
    Quote,
    RiskDecision,
    Side,
)
from algodesk.config.loader import load_run
from algodesk.config.snapshot import build_effective
from algodesk.engine.backtest import BacktestRunner
from algodesk.engine.clock import SimClock
from algodesk.ops.runtime import open_runtime
from algodesk.portfolio.ledger import PortfolioLedger
from algodesk.results.writer import ResultsWriter
from algodesk.risk.engine import RiskEngine, is_reducing_intent
from algodesk.risk.killswitch import KillSwitch
from algodesk.risk.view import RiskViewContext
from tests.conftest import snapshot_guarded_hashes, verify_guarded_hashes
from tests.core.engine.test_m4_1b_backtest_termination import _create_synthetic_backtest_project


def _make_quote(inst: str, ltp: float, ts: datetime) -> Quote:
    return Quote(
        instrument=InstrumentId(inst),
        exch_ts=ts,
        recv_ts=ts,
        bids=(),
        asks=(),
        ltp=ltp,
        volume=1000,
    )


def _build_context(
    clock: SimClock,
    inst_str: str = "NSE:EQ:RELIANCE",
    quote: Quote | None = None,
    params: dict[str, Any] | None = None,
    positions: dict[str, Position] | None = None,
    profile: str = "conservative",
) -> RiskViewContext:
    inst_id = InstrumentId(inst_str)
    inst = Instrument(
        id=inst_id,
        symbol="RELIANCE",
        exchange="NSE",
        segment="EQ",
        kind="EQ",
        lot_size=1,
        tick_size=Decimal("0.05"),
    )
    quotes = {inst_id: quote} if quote is not None else {}
    p_map = (
        params
        if params is not None
        else {
            f"risk.{profile}.stale_data_seconds": 5.0,
            f"risk.{profile}.margin_buffer_pct": 25.0,
            f"risk.{profile}.daily_loss_limit_pct": 1.5,
        }
    )
    pos_map = positions or {}
    ledger = PortfolioLedger(clock=clock, starting_cash=Decimal("100000.00"))
    return RiskViewContext(
        portfolio=ledger.snapshot(),
        positions=pos_map,
        open_orders=[],
        daily_pnl=Decimal("0.00"),
        latest_quotes=quotes,
        params=p_map,
        clock=clock,
        instruments={inst_id: inst},
        allocated_capital=Decimal("100000.00"),
        profile=profile,
    )


# ---------------------------------------------------------------------------
# CP1: Kill-Switch Isolation (Step 4 & 5)
# ---------------------------------------------------------------------------


def test_backtest_does_not_read_armed_real_killswitch(tmp_path: Path) -> None:
    """Step 4 Case 1: Armed real project kill-switch does not block a backtest."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=2, num_sessions=5
    )

    # Arm the project's real killswitch
    real_ks_path = p_dir / "data" / "state" / "killswitch.json"
    real_ks_path.parent.mkdir(parents=True, exist_ok=True)
    armed_data = {
        "active": True,
        "reason": "Emergency market crash halt",
        "by": "manual",
        "ts": "2026-11-20T10:00:00Z",
    }
    real_ks_path.write_text(json.dumps(armed_data), encoding="utf-8")
    init_hash = hashlib.sha256(real_ks_path.read_bytes()).hexdigest()

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=True)
        runner.session_runner._data_service = ds

        # Backtest runner must have its own results-isolated kill-switch path
        expected_ks_file = rt.paths.results / runner.run_id / "killswitch.json"
        assert runner.session_runner.killswitch_file == expected_ks_file

        res_dir = runner.run()

    # Backtest ran cleanly (verdict PASS) despite armed real killswitch
    man = json.loads((res_dir / "manifest.json").read_text(encoding="utf-8"))
    assert man["verdict"] == "PASS"
    assert man["stop_reason"] == "end of data"

    # Real killswitch remains completely untouched and byte-identical
    after_hash = hashlib.sha256(real_ks_path.read_bytes()).hexdigest()
    assert init_hash == after_hash


def test_backtest_daily_loss_trip_leaves_real_killswitch_byte_identical(tmp_path: Path) -> None:
    """Step 4 Case 2: Daily-loss breach in backtest leaves real file byte-identical."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=2, num_sessions=25, sine_period=10.0
    )

    cfg = (p_dir / "config" / "runs" / "test_bt.toml").read_text()
    cfg = cfg.replace("fast = 5, slow = 15, qty = 1", "fast = 2, slow = 4, qty = 100")
    cfg = cfg.replace('end = "2025-01-07"', 'end = "2025-02-15"')
    (p_dir / "config" / "runs" / "test_bt.toml").write_text(cfg)

    # Tighten daily_loss_limit_pct to 0.005% so fill loss breaches limit
    params_file = p_dir / "config" / "params" / "risk_profiles.toml"
    txt = params_file.read_text().replace("default = 1.5\nmin = 0.0", "default = 0.005\nmin = 0.0")
    params_file.write_text(txt)

    # Real project killswitch starts disarmed
    real_ks_path = p_dir / "data" / "state" / "killswitch.json"
    real_ks_path.parent.mkdir(parents=True, exist_ok=True)
    clean_data = {"active": False, "reason": "", "by": "", "ts": None}
    real_ks_path.write_text(json.dumps(clean_data), encoding="utf-8")
    init_hash = hashlib.sha256(real_ks_path.read_bytes()).hexdigest()

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=True)
        runner.session_runner._data_service = ds
        res_dir = runner.run()

    # The run's local killswitch was tripped and recorded inside results folder
    local_ks = res_dir / "killswitch.json"
    assert local_ks.is_file()
    local_data = json.loads(local_ks.read_text(encoding="utf-8"))
    assert local_data["active"] is True
    assert "Daily loss" in local_data["reason"]

    # Real project killswitch remained byte-identical
    after_hash = hashlib.sha256(real_ks_path.read_bytes()).hexdigest()
    assert init_hash == after_hash


def test_paper_mode_uses_real_killswitch_file(tmp_path: Path) -> None:
    """Step 4 Case 3: Paper mode uses real data/state/killswitch.json."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=2, num_sessions=5
    )
    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        from algodesk.engine.runner import SessionRunner

        runner = SessionRunner(runtime=rt, mode=Mode.PAPER, quiet=True)
        expected_real_path = rt.state.params_db.parent / "killswitch.json"
        assert runner.killswitch_file == expected_real_path


def test_live_mode_uses_real_killswitch_file(tmp_path: Path) -> None:
    """Step 4 Case 4: Live mode uses real data/state/killswitch.json."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=2, num_sessions=5
    )
    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        from algodesk.engine.runner import SessionRunner

        runner = SessionRunner(runtime=rt, mode=Mode.LIVE, quiet=True)
        expected_real_path = rt.state.params_db.parent / "killswitch.json"
        assert runner.killswitch_file == expected_real_path


def test_session_guard_detects_killswitch_modification(tmp_path: Path) -> None:
    """Step 5: conftest session guard raises RuntimeError if data/state/killswitch.json changes."""
    ks_path = tmp_path / "data" / "state" / "killswitch.json"
    ks_path.parent.mkdir(parents=True, exist_ok=True)
    ks_path.write_text(json.dumps({"active": False}), encoding="utf-8")

    before = snapshot_guarded_hashes(tmp_path)
    assert "data/state/killswitch.json" in before

    # Mutate killswitch.json
    ks_path.write_text(json.dumps({"active": True, "reason": "tripped"}), encoding="utf-8")
    after = snapshot_guarded_hashes(tmp_path)

    with pytest.raises(RuntimeError, match="Guarded file modified: data/state/killswitch.json"):
        verify_guarded_hashes(before, after)


def test_session_guard_detects_killswitch_addition(tmp_path: Path) -> None:
    """Step 5b: Session guard raises RuntimeError if data/state/killswitch.json is added."""
    before = snapshot_guarded_hashes(tmp_path)
    assert "data/state/killswitch.json" not in before

    ks_path = tmp_path / "data" / "state" / "killswitch.json"
    ks_path.parent.mkdir(parents=True, exist_ok=True)
    ks_path.write_text(json.dumps({"active": True}), encoding="utf-8")

    after = snapshot_guarded_hashes(tmp_path)
    with pytest.raises(RuntimeError, match="Guarded file added: data/state/killswitch.json"):
        verify_guarded_hashes(before, after)


# ---------------------------------------------------------------------------
# CP2: Breach Policy in Backtest (Step 6)
# ---------------------------------------------------------------------------


def test_backtest_on_kill_switch_continue_policy(tmp_path: Path) -> None:
    """Step 6: on_kill_switch = 'continue' runs to end of data, blocks entries, verdict PASS."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=2, num_sessions=25, sine_period=10.0
    )

    cfg = (p_dir / "config" / "runs" / "test_bt.toml").read_text()
    cfg = cfg.replace("fast = 5, slow = 15, qty = 1", "fast = 2, slow = 4, qty = 100")
    cfg = cfg.replace('end = "2025-01-07"', 'end = "2025-02-15"')
    (p_dir / "config" / "runs" / "test_bt.toml").write_text(cfg)

    # Daily loss limit 0.005% triggers trip
    params_file = p_dir / "config" / "params" / "risk_profiles.toml"
    txt = params_file.read_text().replace("default = 1.5\nmin = 0.0", "default = 0.005\nmin = 0.0")
    params_file.write_text(txt)

    # Registry has backtest.on_kill_switch = 'continue'
    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=True)
        runner.session_runner._data_service = ds
        assert runner.session_runner._on_kill_switch == "continue"

        res_dir = runner.run()

    man = json.loads((res_dir / "manifest.json").read_text(encoding="utf-8"))
    metrics = json.loads((res_dir / "metrics.json").read_text(encoding="utf-8"))
    sum_text = (res_dir / "SUMMARY.md").read_text(encoding="utf-8")

    # Run continued to end of data with PASS verdict
    assert man["stop_reason"] == "end of data"
    assert man["verdict"] == "PASS"
    assert metrics["stop_reason"] == "end of data"

    # Metrics contain trip info
    assert metrics.get("kill_switch_tripped_date") is not None
    assert metrics.get("kill_switch_rule") == "daily_loss_limit_pct"
    assert "loss" in metrics.get("kill_switch_values", {})
    assert "limit" in metrics.get("kill_switch_values", {})

    # SUMMARY first line has required format
    assert "KILL-SWITCH tripped on " in sum_text
    assert "daily_loss_limit_pct" in sum_text
    assert "no new entries after this date" in sum_text
    assert "Verdict: PASS" in sum_text
    assert "Feed halted" not in sum_text


def test_backtest_on_kill_switch_stop_policy(tmp_path: Path) -> None:
    """Step 6b: on_kill_switch = 'stop' halts with stop reason 'kill-switch tripped'."""
    p_dir, ds, symbols, sessions = _create_synthetic_backtest_project(
        tmp_path, num_symbols=2, num_sessions=25, sine_period=10.0
    )

    cfg = (p_dir / "config" / "runs" / "test_bt.toml").read_text()
    cfg = cfg.replace("fast = 5, slow = 15, qty = 1", "fast = 2, slow = 4, qty = 100")
    cfg = cfg.replace('end = "2025-01-07"', 'end = "2025-02-15"')
    (p_dir / "config" / "runs" / "test_bt.toml").write_text(cfg)

    # Daily loss limit 0.005% triggers trip
    params_file = p_dir / "config" / "params" / "risk_profiles.toml"
    txt = params_file.read_text().replace("default = 1.5\nmin = 0.0", "default = 0.005\nmin = 0.0")
    params_file.write_text(txt)

    # Set backtest.on_kill_switch = 'stop' in params
    bt_params_file = p_dir / "config" / "params" / "backtest.toml"
    bt_txt = bt_params_file.read_text()
    bt_txt = bt_txt.replace('default = "continue"', 'default = "stop"')
    bt_params_file.write_text(bt_txt)

    with open_runtime(p_dir, run="test_bt", ensure=True) as rt:
        object.__setattr__(rt, "_data_service", ds)
        eff = build_effective(rt.root, run_name="test_bt")
        runner = BacktestRunner(runtime=rt, run_config=eff.run, run_name="test_bt", quiet=True)
        runner.session_runner._data_service = ds
        assert runner.session_runner._on_kill_switch == "stop"

        res_dir = runner.run()

    man = json.loads((res_dir / "manifest.json").read_text(encoding="utf-8"))
    metrics = json.loads((res_dir / "metrics.json").read_text(encoding="utf-8"))
    sum_text = (res_dir / "SUMMARY.md").read_text(encoding="utf-8")

    # Run stopped at trip with reason 'kill-switch tripped', verdict PASS
    assert man["stop_reason"] == "kill-switch tripped"
    assert man["verdict"] == "PASS"
    assert metrics["stop_reason"] == "kill-switch tripped"
    assert "Feed halted" not in sum_text
    assert "KILL-SWITCH tripped on " in sum_text
    assert "no new entries after this date" in sum_text


def test_results_writer_summary_killswitch_line_formatting(tmp_path: Path) -> None:
    """Step 6c: ResultsWriter renders killswitch trip first line conforming to specification."""
    writer = ResultsWriter(run_id="test_ks_format", results_dir=tmp_path)
    summary_info = {
        "run_name": "test_run",
        "mode": "backtest",
        "fidelity": "bar",
        "date": "2026-10-26",
        "verdict": "PASS",
        "fail_reason": None,
    }
    metrics = {
        "trades": 10,
        "net_pnl": 500.0,
        "gross_pnl": 600.0,
        "charges": 100.0,
        "win_rate": 60.0,
        "max_drawdown": 4.5,
        "kill_switch_tripped_date": "2024-10-08",
        "kill_switch_rule": "daily_loss_limit_pct",
        "kill_switch_values": {"loss": 15200.00, "limit": 15000.00, "limit_pct": 1.5},
        "rejects_by_rule": {"killswitch": 4},
    }
    writer.write(
        summary_info=summary_info,
        metrics=metrics,
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={"mode": "backtest"},
    )
    sum_file = tmp_path / "test_ks_format" / "SUMMARY.md"
    assert sum_file.is_file()
    lines = sum_file.read_text(encoding="utf-8").splitlines()

    # The first line of ## Summary must be the KILL-SWITCH line
    summary_start_idx = lines.index("## Summary")
    first_body_line = lines[summary_start_idx + 1]
    assert first_body_line.startswith("KILL-SWITCH tripped on 2024-10-08: daily_loss_limit_pct, ")
    assert "loss=₹15200.00, limit=₹15000.00 (1.5%)" in first_body_line
    assert first_body_line.endswith("no new entries after this date")
    assert "Rejections: 4 (killswitch: 4)" in sum_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CP3: Swing Profile Configuration (Step 7)
# ---------------------------------------------------------------------------


def test_swing_risk_profile_parameters() -> None:
    """Step 7: Swing profile in risk_profiles.toml has specified values."""
    from algodesk.params.registry import ParamRegistry

    reg = ParamRegistry(":memory:")
    reg.load_defaults(Path("config/params"))
    assert float(reg.get("risk.swing.daily_loss_limit_pct")) == 4.0
    assert float(reg.get("risk.swing.weekly_loss_limit_pct")) == 8.0
    assert float(reg.get("risk.swing.drawdown_halt_pct")) == 15.0
    assert float(reg.get("risk.swing.per_trade_risk_pct")) == 0.5
    assert float(reg.get("risk.swing.max_position_pct")) == 20.0
    assert int(reg.get("risk.swing.max_open_positions")) == 5


def test_rotation_1d_bt_config_uses_swing_profile() -> None:
    """Step 7b: config/runs/rotation_1d_bt.toml configures risk_profile = 'swing'."""
    cfg = load_run(Path("."), "rotation_1d_bt")
    assert len(cfg.strategies) == 1
    assert cfg.strategies[0].risk_profile == "swing"


# ---------------------------------------------------------------------------
# CP4: Risk Engine Safety (Step 8)
# ---------------------------------------------------------------------------


def test_reducing_intent_qty_lte_held_is_reducing() -> None:
    """Step 8: SELL of held long with qty <= held is recognized as reducing intent."""
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    pos = Position(instrument=inst_id, product=Product.CNC, qty=100, avg_price=Decimal("2500.00"))
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    view = _build_context(clock=clock, positions={f"{inst_id}:CNC": pos})

    spec = OrderSpec(
        instrument=inst_id,
        side=Side.SELL,
        qty=100,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    intent = PlaceOrder(intent_id="reduce_full", spec=spec)
    assert is_reducing_intent(intent, view) is True

    # Partial reduction
    spec_partial = OrderSpec(
        instrument=inst_id,
        side=Side.SELL,
        qty=50,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    intent_partial = PlaceOrder(intent_id="reduce_part", spec=spec_partial)
    assert is_reducing_intent(intent_partial, view) is True


def test_oversized_sell_is_not_reducing_intent() -> None:
    """Step 8b: SELL with qty > held quantity is NOT a reducing intent."""
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    pos = Position(instrument=inst_id, product=Product.CNC, qty=100, avg_price=Decimal("2500.00"))
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    view = _build_context(clock=clock, positions={f"{inst_id}:CNC": pos})

    # Sell 150 when holding 100
    spec_oversized = OrderSpec(
        instrument=inst_id,
        side=Side.SELL,
        qty=150,
        type=OrderType.MARKET,
        product=Product.CNC,
    )
    intent_oversized = PlaceOrder(intent_id="oversized_sell", spec=spec_oversized)
    assert is_reducing_intent(intent_oversized, view) is False


def test_reducing_intent_skips_entry_only_rules(tmp_path: Path) -> None:
    """Step 8c: Reducing intent bypasses entry-only rules."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    ks = KillSwitch(state_file=tmp_path / "ks.json", clock=clock)

    # Create dummy rule that blocks entries
    class MockEntryRule:
        name = "cash_available"

        def check(self, intent: Any, view: Any) -> RiskDecision:
            return RiskDecision(allow=False, rule="cash_available", reason="Zero cash")

    engine = RiskEngine(killswitch=ks, rules=[MockEntryRule()])
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    pos = Position(instrument=inst_id, product=Product.CNC, qty=100, avg_price=Decimal("2500.00"))
    quote = _make_quote(str(inst_id), 2500.0, now)
    view = _build_context(clock=clock, quote=quote, positions={f"{inst_id}:CNC": pos})

    intent = PlaceOrder(
        intent_id="exit_sell",
        spec=OrderSpec(
            instrument=inst_id,
            side=Side.SELL,
            qty=100,
            type=OrderType.MARKET,
            product=Product.CNC,
        ),
    )
    decision = engine.evaluate(intent, view)
    assert decision.allow is True
    assert decision.rule == "all_rules_passed"


def test_reducing_intent_strictly_blocked_by_killswitch_in_default_mode(tmp_path: Path) -> None:
    """Step 8d: Reducing intent is blocked by kill-switch in default/paper mode."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    ks = KillSwitch(state_file=tmp_path / "ks.json", clock=clock)
    ks.trip(reason="Market halt")

    engine = RiskEngine(killswitch=ks, rules=[], allow_reducing_on_killswitch=False)
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    pos = Position(instrument=inst_id, product=Product.CNC, qty=100, avg_price=Decimal("2500.00"))
    quote = _make_quote(str(inst_id), 2500.0, now)
    view = _build_context(clock=clock, quote=quote, positions={f"{inst_id}:CNC": pos})

    intent = PlaceOrder(
        intent_id="exit_sell_ks",
        spec=OrderSpec(
            instrument=inst_id,
            side=Side.SELL,
            qty=100,
            type=OrderType.MARKET,
            product=Product.CNC,
        ),
    )
    decision = engine.evaluate(intent, view)
    assert decision.allow is False
    assert decision.rule == "killswitch"
    assert "Kill-switch active" in decision.reason


def test_reducing_intent_blocked_by_missing_quote(tmp_path: Path) -> None:
    """Step 8e: Reducing intent is blocked by missing market quote."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    ks = KillSwitch(state_file=tmp_path / "ks.json", clock=clock)
    engine = RiskEngine(killswitch=ks)

    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    pos = Position(instrument=inst_id, product=Product.CNC, qty=100, avg_price=Decimal("2500.00"))
    view = _build_context(clock=clock, quote=None, positions={f"{inst_id}:CNC": pos})

    intent = PlaceOrder(
        intent_id="exit_no_quote",
        spec=OrderSpec(
            instrument=inst_id,
            side=Side.SELL,
            qty=100,
            type=OrderType.MARKET,
            product=Product.CNC,
        ),
    )
    decision = engine.evaluate(intent, view)
    assert decision.allow is False
    assert decision.rule == "data_integrity.missing_quote"


def test_reducing_intent_blocked_by_stale_quote(tmp_path: Path) -> None:
    """Step 8f: Reducing intent is blocked by stale market quote."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    stale_ts = now - timedelta(seconds=15)
    clock = SimClock(now)
    ks = KillSwitch(state_file=tmp_path / "ks.json", clock=clock)
    engine = RiskEngine(killswitch=ks)

    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    pos = Position(instrument=inst_id, product=Product.CNC, qty=100, avg_price=Decimal("2500.00"))
    quote = _make_quote(str(inst_id), 2500.0, stale_ts)  # 15s old, stale_limit is 5s
    view = _build_context(clock=clock, quote=quote, positions={f"{inst_id}:CNC": pos})

    intent = PlaceOrder(
        intent_id="exit_stale_quote",
        spec=OrderSpec(
            instrument=inst_id,
            side=Side.SELL,
            qty=100,
            type=OrderType.MARKET,
            product=Product.CNC,
        ),
    )
    decision = engine.evaluate(intent, view)
    assert decision.allow is False
    assert decision.rule == "data_integrity.stale_data"


def test_reducing_intent_blocked_by_unknown_instrument(tmp_path: Path) -> None:
    """Step 8g: Reducing intent is blocked if instrument is unknown to universe."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    ks = KillSwitch(state_file=tmp_path / "ks.json", clock=clock)
    engine = RiskEngine(killswitch=ks)

    unknown_id = InstrumentId("NSE:EQ:UNKNOWN")
    pos = Position(
        instrument=unknown_id, product=Product.CNC, qty=100, avg_price=Decimal("2500.00")
    )
    quote = _make_quote(str(unknown_id), 100.0, now)
    view = _build_context(clock=clock, quote=quote, positions={f"{unknown_id}:CNC": pos})

    intent = PlaceOrder(
        intent_id="exit_unknown_inst",
        spec=OrderSpec(
            instrument=unknown_id,
            side=Side.SELL,
            qty=100,
            type=OrderType.MARKET,
            product=Product.CNC,
        ),
    )
    decision = engine.evaluate(intent, view)
    assert decision.allow is False
    assert decision.rule == "data_integrity.unknown_instrument"


def test_backtest_continue_mode_allows_reducing_orders_on_killswitch(tmp_path: Path) -> None:
    """Step 8h: Backtest continue policy allows reducing orders even when killswitch tripped."""
    now = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    clock = SimClock(now)
    ks = KillSwitch(state_file=tmp_path / "ks.json", clock=clock)
    ks.trip(reason="Daily loss limit hit")

    engine = RiskEngine(killswitch=ks, rules=[], allow_reducing_on_killswitch=True)
    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    pos = Position(instrument=inst_id, product=Product.CNC, qty=100, avg_price=Decimal("2500.00"))
    quote = _make_quote(str(inst_id), 2500.0, now)
    view = _build_context(clock=clock, quote=quote, positions={f"{inst_id}:CNC": pos})

    # Reducing order is allowed
    intent_reduce = PlaceOrder(
        intent_id="exit_allowed",
        spec=OrderSpec(
            instrument=inst_id,
            side=Side.SELL,
            qty=100,
            type=OrderType.MARKET,
            product=Product.CNC,
        ),
    )
    decision = engine.evaluate(intent_reduce, view)
    assert decision.allow is True
    assert decision.rule == "all_rules_passed"

    # New entry order is still strictly blocked
    intent_entry = PlaceOrder(
        intent_id="new_entry_blocked",
        spec=OrderSpec(
            instrument=inst_id,
            side=Side.BUY,
            qty=10,
            type=OrderType.MARKET,
            product=Product.CNC,
        ),
    )
    decision_entry = engine.evaluate(intent_entry, view)
    assert decision_entry.allow is False
    assert decision_entry.rule == "killswitch"
