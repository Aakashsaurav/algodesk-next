"""Tests for M4-5b: Rotation two-step rebalance, daily mark equity, and metrics.

Requirements: FR-BKT-5, FR-RPT-3, FR-STR-1, 3, INV-3, ADR-089, ADR-090.
"""

from __future__ import annotations

import csv
import json
import math
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from plugins.strategies.screen_rotation.plugin import ScreenRotationStrategy

from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar
from algodesk.api.protocols import ScreenSelection, StrategyContext
from algodesk.api.trading import (
    Fill,
    InstrumentId,
    OrderSpec,
    OrderType,
    PlaceOrder,
    Position,
    Product,
    Side,
    Validity,
)
from algodesk.results.compare import compare_runs
from algodesk.results.metrics import (
    compute_daily_returns,
    compute_drawdown_metrics,
    compute_round_trip_performance,
    compute_sharpe,
    compute_sortino,
)
from algodesk.results.writer import ResultsWriter
from algodesk.risk.engine import RiskEngine
from algodesk.risk.killswitch import KillSwitch


class DummyRiskClock:
    def now(self) -> datetime:
        return datetime(2025, 1, 1, 10, 0, tzinfo=UTC)


class DummyRiskQuote:
    ltp = Decimal("100.00")
    bid = Decimal("100.00")
    ask = Decimal("100.00")
    recv_ts = datetime(2025, 1, 1, 10, 0, tzinfo=UTC)


class DummyRiskPortfolio:
    def __init__(
        self,
        cash: Decimal = Decimal("100000.00"),
        equity: Decimal = Decimal("100000.00"),
    ) -> None:
        self.cash = cash
        self.equity = equity


class DummyRiskView:
    def __init__(
        self,
        cash: Decimal = Decimal("0.00"),
        positions: dict[str, Position] | None = None,
        allocated_capital: Decimal = Decimal("100000.00"),
    ) -> None:
        self.profile = "conservative"
        self.positions = positions or {}
        self.portfolio = DummyRiskPortfolio(cash=cash, equity=allocated_capital)
        self.allocated_capital = allocated_capital
        self.params = {
            "margin_buffer_pct": 25.0,
            "risk.conservative.margin_buffer_pct": 25.0,
            "risk.conservative.stale_data_seconds": 60.0,
        }
        self.instruments: dict[str, Any] = {}
        self.session_manager = None
        self.clock = DummyRiskClock()

    def quote_for(self, inst_id: Any) -> Any:
        return DummyRiskQuote()


class DummyPortfolio:
    def __init__(self, equity: float = 100000.0, cash: float | None = None) -> None:
        self.equity = Decimal(str(equity))
        self.cash = Decimal(str(cash if cash is not None else equity))
        self.starting_cash = Decimal(str(equity))
        self.positions: dict[str, Position] = {}


def _make_bar(inst: str, end_ts: datetime, close: float = 100.0) -> Bar:
    return Bar(
        instrument=InstrumentId(inst),
        timeframe="1d",
        start_ts=end_ts.replace(hour=3, minute=45),
        end_ts=end_ts,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000,
        complete=True,
        synthetic=False,
    )


def _make_manifest() -> PluginManifest:
    return PluginManifest(
        name="screen_rotation",
        version="0.1.0",
        kind="strategy",  # type: ignore[arg-type]
        api_version=1,
        entry="plugin:ScreenRotationStrategy",
        capabilities={"backtest": True},
    )


# =========================================================================
# CP1: Two-step rebalance tests (Steps 4, 5, 6)
# =========================================================================


def test_two_step_rebalance_sells_first_then_replacement_buys() -> None:
    """Step 4: Sells emitted at rebalance close; replacement buys deferred until fills."""
    strat = ScreenRotationStrategy(
        _make_manifest(),
        {"screen": "top_screen", "top_n": 2, "weight_pct": 15.0, "rebalance_every": 2},
    )

    # Initial selection: SYM_A and SYM_B
    sel1 = ScreenSelection(
        symbols=("NSE:EQ:SYM_A", "NSE:EQ:SYM_B"),
        scores={"NSE:EQ:SYM_A": 90.0, "NSE:EQ:SYM_B": 80.0},
        as_of=date(2024, 1, 2),
        result_hash="hash1",
    )
    positions: dict[str, Position] = {}
    portfolio = DummyPortfolio(100000.0)

    ctx1 = StrategyContext(
        screen=lambda name, *, group=None: sel1,
        bars=lambda inst, tf, n: (
            _make_bar(inst, datetime(2024, 1, 2, 10, 0, tzinfo=UTC), close=100.0),
        ),
        position=lambda inst: positions.get(str(inst)),
        portfolio=portfolio,
    )

    # Day 1: Buys generated directly since no positions held
    intents1 = strat.on_bar(
        ctx1, _make_bar("NSE:EQ:SYM_A", datetime(2024, 1, 2, 10, 0, tzinfo=UTC))
    )
    assert len(intents1) == 2
    for intent in intents1:
        assert isinstance(intent, PlaceOrder)
        assert intent.spec.side == Side.BUY
        # Simulate fill
        positions[str(intent.spec.instrument)] = Position(
            instrument=intent.spec.instrument,
            product=Product.CNC,
            qty=intent.spec.qty,
            avg_price=Decimal("100.00"),
        )
        strat.on_fill(
            ctx1,
            Fill(
                fill_id=f"f_{intent.spec.instrument}",
                client_ref=intent.intent_id,
                instrument=intent.spec.instrument,
                side=Side.BUY,
                qty=intent.spec.qty,
                price=Decimal("100.00"),
                ts=datetime(2024, 1, 2, 10, 1, tzinfo=UTC),
            ),
        )

    # Day 2: Session 2 -> no rebalance
    intents2 = strat.on_bar(
        ctx1, _make_bar("NSE:EQ:SYM_A", datetime(2024, 1, 3, 10, 0, tzinfo=UTC))
    )
    assert len(intents2) == 0

    # Day 3: Rebalance session! SYM_B drops out; SYM_C enters. SYM_A stays.
    sel2 = ScreenSelection(
        symbols=("NSE:EQ:SYM_A", "NSE:EQ:SYM_C"),
        scores={"NSE:EQ:SYM_A": 95.0, "NSE:EQ:SYM_C": 85.0},
        as_of=date(2024, 1, 4),
        result_hash="hash2",
    )
    ctx2 = StrategyContext(
        screen=lambda name, *, group=None: sel2,
        bars=lambda inst, tf, n: (
            _make_bar(inst, datetime(2024, 1, 4, 10, 0, tzinfo=UTC), close=100.0),
        ),
        position=lambda inst: positions.get(str(inst)),
        portfolio=portfolio,
    )

    # Step 1: On rebalance bar, ONLY SELL of SYM_B is emitted
    intents3 = strat.on_bar(
        ctx2, _make_bar("NSE:EQ:SYM_A", datetime(2024, 1, 4, 10, 0, tzinfo=UTC))
    )
    assert len(intents3) == 1
    assert isinstance(intents3[0], PlaceOrder)
    assert intents3[0].spec.side == Side.SELL
    assert intents3[0].spec.instrument == InstrumentId("NSE:EQ:SYM_B")

    # While sell is pending, on_bar does NOT emit buys
    intents_mid = strat.on_bar(
        ctx2, _make_bar("NSE:EQ:SYM_A", datetime(2024, 1, 4, 10, 5, tzinfo=UTC))
    )
    assert len(intents_mid) == 0

    # Sell fills!
    del positions["NSE:EQ:SYM_B"]
    strat.on_fill(
        ctx2,
        Fill(
            fill_id="f_sell_b",
            client_ref=intents3[0].intent_id,
            instrument=InstrumentId("NSE:EQ:SYM_B"),
            side=Side.SELL,
            qty=intents3[0].spec.qty,
            price=Decimal("100.00"),
            ts=datetime(2024, 1, 4, 10, 6, tzinfo=UTC),
        ),
    )

    # Step 2: Next completed bar emits replacement BUY of SYM_C
    intents4 = strat.on_bar(
        ctx2, _make_bar("NSE:EQ:SYM_A", datetime(2024, 1, 4, 10, 10, tzinfo=UTC))
    )
    assert len(intents4) == 1
    assert isinstance(intents4[0], PlaceOrder)
    assert intents4[0].spec.side == Side.BUY
    assert intents4[0].spec.instrument == InstrumentId("NSE:EQ:SYM_C")


def test_rebalances_csv_columns_and_execution_fidelity(tmp_path: Path) -> None:
    """Step 6: rebalances.csv has 8 columns and records fills (not intents)."""
    writer = ResultsWriter(run_id="reb_test", results_dir=tmp_path)
    rebalances = [
        {
            "rebalance_date": "2024-01-02",
            "screen_result_hash": "hash_001",
            "selected_symbols": "SYM_A:90.00;SYM_B:80.00",
            "intended_sold": [],
            "intended_bought": ["SYM_A", "SYM_B"],
            "executed_sold": [],
            "executed_bought": ["SYM_A", "SYM_B"],
            "rejected": [],
        },
        {
            "rebalance_date": "2024-02-01",
            "screen_result_hash": "hash_002",
            "selected_symbols": "SYM_A:95.00;SYM_C:85.00",
            "intended_sold": ["SYM_B"],
            "intended_bought": ["SYM_C"],
            "executed_sold": ["SYM_B"],
            "executed_bought": ["SYM_C"],
            "rejected": ["SYM_D:cash_available"],
        },
    ]

    writer._write_rebalances_csv(rebalances)
    csv_file = tmp_path / "reb_test" / "rebalances.csv"
    assert csv_file.is_file()

    with open(csv_file, newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))

    assert len(reader) == 2
    row0 = reader[0]
    assert row0["rebalance_date"] == "2024-01-02"
    assert row0["screen_result_hash"] == "hash_001"
    assert row0["selected_symbols"] == "SYM_A:90.00;SYM_B:80.00"
    assert row0["intended_sold"] == ""
    assert row0["intended_bought"] == "SYM_A;SYM_B"
    assert row0["executed_sold"] == ""
    assert row0["executed_bought"] == "SYM_A;SYM_B"
    assert row0["rejected"] == ""

    row1 = reader[1]
    assert row1["rebalance_date"] == "2024-02-01"
    assert row1["intended_sold"] == "SYM_B"
    assert row1["executed_sold"] == "SYM_B"
    assert row1["intended_bought"] == "SYM_C"
    assert row1["executed_bought"] == "SYM_C"
    assert row1["rejected"] == "SYM_D:cash_available"


def test_kill_switch_strictly_blocks_rotation_sell(tmp_path: Path) -> None:
    """Safety Property: SELL of held long position NEVER bypasses kill-switch."""
    ks = KillSwitch(state_file=tmp_path / "ks.json")
    risk = RiskEngine(killswitch=ks, rules=())
    intent = PlaceOrder(
        spec=OrderSpec(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            side=Side.SELL,
            qty=100,
            type=OrderType.MARKET,
            product=Product.CNC,
            validity=Validity.DAY,
        ),
        intent_id="rot_sell_killswitch_test",
    )
    pos = Position(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        product=Product.CNC,
        qty=100,
        avg_price=Decimal("2500.00"),
    )
    view = DummyRiskView(positions={"NSE:EQ:RELIANCE:CNC": pos})

    # Inactive kill-switch -> allowed (no rules)
    dec = risk.evaluate(intent, view)
    assert dec.allow is True

    # Activate kill-switch
    ks.trip(reason="Manual trigger", by="test")
    dec_killed = risk.evaluate(intent, view)
    assert dec_killed.allow is False
    assert dec_killed.rule == "killswitch"
    assert "kill-switch active" in dec_killed.reason.lower()


def test_reducing_sells_bypass_entry_only_rules(tmp_path: Path) -> None:
    """SELL of held long position bypasses entry-only rules like cash_available."""
    from plugins.risk_rules.builtin.rules import CashAvailableRule

    ks = KillSwitch(state_file=tmp_path / "ks.json")
    risk = RiskEngine(killswitch=ks, rules=[CashAvailableRule()])
    intent = PlaceOrder(
        spec=OrderSpec(
            instrument=InstrumentId("NSE:EQ:INFY"),
            side=Side.SELL,
            qty=50,
            type=OrderType.MARKET,
            product=Product.CNC,
            validity=Validity.DAY,
        ),
        intent_id="rot_sell_reduce_test",
    )
    pos = Position(
        instrument=InstrumentId("NSE:EQ:INFY"),
        product=Product.CNC,
        qty=50,
        avg_price=Decimal("1500.00"),
    )
    # Cash is zero! CashAvailableRule would normally reject an entry order
    view = DummyRiskView(cash=Decimal("0.00"), positions={"NSE:EQ:INFY:CNC": pos})
    dec = risk.evaluate(intent, view)
    assert dec.allow is True


# =========================================================================
# CP2: Daily equity mark & Hand-computed 2-stock fixture (Step 7, 8)
# =========================================================================


def test_hand_computed_two_stock_equity_and_drawdown_metrics() -> None:
    """Step 7: 2-stock hand-computed fixture with explicit arithmetic comments.

    Fixture Setup:
      Initial Cash = 100,000.00
      Two stocks: Stock A ("NSE:EQ:A") and Stock B ("NSE:EQ:B").
      Day 1:
        Buy 100 shares of A @ 100.00 -> cost = 10,000.00
        Buy 200 shares of B @ 50.00  -> cost = 10,000.00
        Total spent = 20,000.00
        Remaining cash = 80,000.00
        Day 1 close: A @ 100.00, B @ 50.00
        Equity_1 = Cash + (100 * 100.00) + (200 * 50.00)
                 = 80,000.00 + 10,000.00 + 10,000.00 = 100,000.00

      Day 2 (no fills, prices rise):
        Day 2 close: A @ 110.00, B @ 55.00
        Equity_2 = 80,000.00 + (100 * 110.00) + (200 * 55.00)
                 = 80,000.00 + 11,000.00 + 11,000.00 = 102,000.00

      Day 3 (no fills, prices fall into drawdown):
        Day 3 close: A @ 90.00, B @ 45.00
        Equity_3 = 80,000.00 + (100 * 90.00) + (200 * 45.00)
                 = 80,000.00 + 9,000.00 + 9,000.00 = 98,000.00

      Day 4 (no fills, partial recovery):
        Day 4 close: A @ 95.00, B @ 50.00
        Equity_4 = 80,000.00 + (100 * 95.00) + (200 * 50.00)
                 = 80,000.00 + 9,500.00 + 10,000.00 = 99,500.00

      Day 5 (no fills, full recovery and new peak):
        Day 5 close: A @ 120.00, B @ 60.00
        Equity_5 = 80,000.00 + (100 * 120.00) + (200 * 60.00)
                 = 80,000.00 + 12,000.00 + 12,000.00 = 104,000.00

    Daily returns calculation (n = 4):
      r1 = (102,000 - 100,000) / 100,000 = +0.02 (+2.0000%)
      r2 = (98,000 - 102,000) / 102,000   = -4,000 / 102,000 = -0.039215686 (-3.9216%)
      r3 = (99,500 - 98,000) / 98,000     = +1,500 / 98,000  = +0.015306122 (+1.5306%)
      r4 = (104,000 - 99,500) / 99,500   = +4,500 / 99,500  = +0.045226131 (+4.5226%)

    Drawdown calculation:
      Peak progression:
        Day 1: peak = 100,000.00
        Day 2: peak = 102,000.00 (index 1)
        Day 3: equity = 98,000.00 -> dd = 4,000 / 102,000 = 3.9215686%
        Day 4: equity = 99,500.00 -> dd = (102,000 - 99,500) / 102,000 = 2,500 / 102,000 = 2.45098%
        Day 5: equity = 104,000.00 -> new peak! Recovered from peak at index 1.
      Max Drawdown = 3.9216%
      Drawdown Duration = index 4 - index 1 = 3 sessions (or 3 calendar days).

    Sharpe ratio calculation:
      mean_r = (0.02 - 0.039215686 + 0.015306122 + 0.045226131) / 4 = 0.01032914175
      var (ddof=1) = sum((r_i - mean_r)^2) / 3 = 0.001292025 / 3 = 0.000430675
      std = sqrt(var) = 0.03576634
      annualized Sharpe = (mean_r / std) * sqrt(252) = (0.01032914 / 0.03576634) * 15.87450786
                        = 0.288795 * 15.87450786 = 4.5845

    Sortino ratio calculation:
      Only r2 is below rf=0.0:
      downside_sq = [0.0, (-0.039215686)^2, 0.0, 0.0] = [0.0, 0.00153787, 0.0, 0.0]
      downside_var (ddof=1) = 0.00153787 / 3 = 0.0005126233
      downside_dev = sqrt(0.0005126233) = 0.022641187
      annualized Sortino = (mean_r / downside_dev) * sqrt(252)
                         = (0.01032914 / 0.022641187) * 15.87450786
                         = 0.45621 * 15.87450786 = 7.2421
    """
    equities = [
        Decimal("100000.00"),
        Decimal("102000.00"),
        Decimal("98000.00"),
        Decimal("99500.00"),
        Decimal("104000.00"),
    ]
    timestamps = [
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 1, 2, tzinfo=UTC),
        datetime(2025, 1, 3, tzinfo=UTC),
        datetime(2025, 1, 4, tzinfo=UTC),
        datetime(2025, 1, 5, tzinfo=UTC),
    ]

    # Verify daily returns
    d_returns = compute_daily_returns(equities)
    assert len(d_returns) == 4
    assert math.isclose(d_returns[0], 0.02, abs_tol=1e-5)
    assert math.isclose(d_returns[1], -4000.0 / 102000.0, abs_tol=1e-5)
    assert math.isclose(d_returns[2], 1500.0 / 98000.0, abs_tol=1e-5)
    assert math.isclose(d_returns[3], 4500.0 / 99500.0, abs_tol=1e-5)

    # Verify drawdown
    max_dd, duration = compute_drawdown_metrics(equities, timestamps=timestamps)
    assert max_dd == 3.9216
    assert duration == 3

    # Verify Sharpe and Sortino
    sharpe = compute_sharpe(d_returns, risk_free_rate_pct=0.0, trading_days_per_year=252)
    sortino = compute_sortino(d_returns, risk_free_rate_pct=0.0, trading_days_per_year=252)
    assert sharpe is not None
    assert sortino is not None
    assert math.isclose(sharpe, 4.6128, abs_tol=1e-2)
    assert math.isclose(sortino, 7.2421, abs_tol=1e-2)


def test_sortino_returns_none_when_downside_deviation_is_zero() -> None:
    """Step 8: Sortino when downside deviation is zero returns None."""
    # All positive returns -> no return below rf=0
    returns = [0.01, 0.02, 0.03, 0.015]
    sortino = compute_sortino(returns, risk_free_rate_pct=0.0)
    assert sortino is None


def test_profit_factor_none_only_when_no_losses() -> None:
    """Step 8: Profit factor is None only when there are no losses."""
    # Only wins -> profit factor is None
    wins_only = [{"net_pnl": Decimal("100.00")}, {"net_pnl": Decimal("50.00")}]
    perf_w = compute_round_trip_performance(wins_only)
    assert perf_w["profit_factor"] is None

    # Wins and losses -> numeric profit factor
    mixed = [{"net_pnl": Decimal("100.00")}, {"net_pnl": Decimal("-50.00")}]
    perf_m = compute_round_trip_performance(mixed)
    assert perf_m["profit_factor"] == 2.0

    # Only losses -> 0.0 (not None, because losses exist)
    loss_only = [{"net_pnl": Decimal("-50.00")}]
    perf_l = compute_round_trip_performance(loss_only)
    assert perf_l["profit_factor"] == 0.0


def test_summary_md_shows_rejections_and_drift_assumptions(tmp_path: Path) -> None:
    """Step 5 & 8: SUMMARY.md displays reject count line and drift assumption."""
    writer = ResultsWriter(run_id="summary_test", results_dir=tmp_path)
    summary_info = {
        "run_name": "rotation_test_run",
        "mode": "backtest",
        "date": "2024-01-02",
        "verdict": "PASS",
        "screen_name": "trend_rank",
    }
    metrics = {
        "trades": 12,
        "net_pnl": 15000.0,
        "gross_pnl": 15500.0,
        "charges": 500.0,
        "win_rate": 66.7,
        "max_drawdown": 5.2,
        "closed_round_trips": 6,
        "rejects_by_rule": {"cash_available": 3, "duplicate_entry_guard": 1},
    }

    writer._write_summary(summary_info, metrics)

    sum_file = tmp_path / "summary_test" / "SUMMARY.md"
    assert sum_file.is_file()
    text = sum_file.read_text(encoding="utf-8")

    # Reject line present
    assert "Rejections: 4 (cash_available: 3, duplicate_entry_guard: 1)." in text

    # Drift assumption present
    assert "weights not re-balanced between rotations (positions keep their drifted size)" in text

    # Word count <= 150
    words = text.split()
    assert len(words) <= 150


def test_summary_md_shows_zero_rejections_when_none(tmp_path: Path) -> None:
    """Step 8: SUMMARY.md shows Rejections: 0. when no rejections occurred."""
    writer = ResultsWriter(run_id="zero_rej_test", results_dir=tmp_path)
    summary_info = {
        "run_name": "zero_rej_run",
        "mode": "backtest",
        "date": "2024-01-02",
        "verdict": "PASS",
    }
    metrics = {
        "trades": 2,
        "net_pnl": 500.0,
        "gross_pnl": 520.0,
        "charges": 20.0,
        "win_rate": 100.0,
        "max_drawdown": 1.0,
        "closed_round_trips": 1,
        "rejects_by_rule": {},
    }

    writer._write_summary(summary_info, metrics)
    sum_file = tmp_path / "zero_rej_test" / "SUMMARY.md"
    text = sum_file.read_text(encoding="utf-8")
    assert "Rejections: 0." in text


def test_sortino_returns_none_when_fewer_than_two_periods() -> None:
    """Step 8: Sortino is None when return series has fewer than 2 observations."""
    assert compute_sortino([]) is None
    assert compute_sortino([0.05]) is None


def test_compare_runs_handles_none_sortino(tmp_path: Path) -> None:
    """Step 8: Results compare handles None sortino formatted as 'None (no downside periods)'."""
    results_root = tmp_path / "results"
    dir_a = results_root / "run_a"
    dir_b = results_root / "run_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    meta_a = {
        "verdict": "PASS",
        "code_version": "0.1.0",
        "result_hash": "hash_a",
        "stop_reason": "end of data",
    }
    meta_b = {
        "verdict": "PASS",
        "code_version": "0.1.0",
        "result_hash": "hash_a",
        "stop_reason": "end of data",
    }
    (dir_a / "manifest.json").write_text(json.dumps(meta_a))
    (dir_b / "manifest.json").write_text(json.dumps(meta_b))

    metrics_a = {
        "net_pnl": 100.0,
        "trades": 2,
        "sortino": None,
        "sharpe": 1.5,
        "closed_round_trips": 1,
        "win_rate": 100.0,
        "max_drawdown": 0.5,
    }
    metrics_b = {
        "net_pnl": 100.0,
        "trades": 2,
        "sortino": None,
        "sharpe": 1.5,
        "closed_round_trips": 1,
        "win_rate": 100.0,
        "max_drawdown": 0.5,
    }
    (dir_a / "metrics.json").write_text(json.dumps(metrics_a))
    (dir_b / "metrics.json").write_text(json.dumps(metrics_b))

    equal, reason = compare_runs(tmp_path, "run_a", "run_b")
    assert equal is True
    assert reason is None
