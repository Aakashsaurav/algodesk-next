"""Tests for M4-5d: Backtest daily-loss baseline verification and fix (ADR-096)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from algodesk.api.kinds import Mode
from algodesk.portfolio.ledger import PortfolioLedger
from algodesk.risk.killswitch import KillSwitch
from algodesk.risk.monitor import RiskMonitor


def test_fixture_equity_path_known_cases(tmp_path: Path) -> None:
    """Fixture test with known equity path (Step 4 & 5):

    - A -3.9% day does not trip a 4% daily limit.
    - A -4.1% day trips a 4% daily limit.
    - A multi-day slide of -1% per day for 8 days does not trip a 4% daily limit.
    """
    ks_path = tmp_path / "killswitch.json"
    ks = KillSwitch(state_file=ks_path)
    ledger = PortfolioLedger(starting_cash=Decimal("1000000.00"))
    params = {"risk.swing.daily_loss_limit_pct": 4.0}

    monitor = RiskMonitor(
        killswitch=ks,
        portfolio=ledger,
        params=params,
        profile="swing",
        allocated_capital=Decimal("1000000.00"),
    )

    # Day 1: starts at 1,000,000. Drops by 3.9% (-39,000 -> equity 961,000)
    ledger.starting_cash = Decimal("961000.00")
    assert monitor.check_daily_loss() is False
    assert ks.is_active() is False

    # Day 1 close: baseline for Day 2 is set to Day 1 closing equity (961,000)
    ledger.set_start_of_day_baseline(ledger.equity)
    assert ledger._start_of_day_equity == Decimal("961000.00")

    # Multi-day slide of -1% per day (of 1,000,000 capital, i.e. -10,000/day) for 8 days
    # Day 2 to Day 9: each day drops 10,000 from start of that day.
    for _day in range(2, 10):
        prev_close = ledger.equity
        ledger.set_start_of_day_baseline(prev_close)
        # Drop by 1% of capital = 10,000
        ledger.starting_cash -= Decimal("10000.00")
        assert monitor.check_daily_loss() is False
        assert ks.is_active() is False

    # Verify total cumulative drop across the 8 days was 80,000 (-8%)
    # yet 4% daily limit NEVER tripped because each day's loss was only 10,000 (1%).
    assert ledger.equity == Decimal("961000.00") - Decimal("80000.00")

    # Now Day 10: reset baseline to Day 9 close
    ledger.set_start_of_day_baseline(ledger.equity)

    # Day 10: drops by -4.1% of capital (41,000.00)
    ledger.starting_cash -= Decimal("41000.00")
    tripped = monitor.check_daily_loss()
    assert tripped is True
    assert ks.is_active() is True
    st = ks.status()
    assert st["active"] is True
    assert st["rule"] == "daily_loss_limit_pct"
    assert st["values"]["loss"] == 41000.0
    assert st["values"]["limit"] == 40000.0


def test_set_start_of_day_baseline_explicit_and_default() -> None:
    """PortfolioLedger.set_start_of_day_baseline respects explicit value and defaults to equity."""
    ledger = PortfolioLedger(starting_cash=Decimal("500000.00"))
    assert ledger._start_of_day_equity == Decimal("500000.00")

    # Set explicit baseline
    ledger.set_start_of_day_baseline(Decimal("520000.00"))
    assert ledger._start_of_day_equity == Decimal("520000.00")
    assert ledger.day_pnl == Decimal("-20000.00")

    # Change equity and set default baseline
    ledger.starting_cash = Decimal("480000.00")
    ledger.set_start_of_day_baseline()
    assert ledger._start_of_day_equity == Decimal("480000.00")
    assert ledger.day_pnl == Decimal("0.00")


def test_paper_mode_baseline_behavior_untouched(tmp_path: Path) -> None:
    """In paper mode, SessionRunner relies on timers and leaves backtest baseline untouched."""
    from algodesk.engine.runner import SessionRunner

    mock_runtime = MagicMock()
    mock_runtime.effective.run = None
    mock_runtime.paths.results = tmp_path / "results"

    sr = SessionRunner(
        runtime=mock_runtime,
        run_name="demo_paper",
        mode=Mode.PAPER,
    )
    # _on_backtest_market_event hook is no-op unless backtest
    assert sr.mode == Mode.PAPER
    assert sr._is_replay is False


def test_backtest_session_baseline_reset_on_date_boundary(tmp_path: Path) -> None:
    """BacktestRunner._install_daily_mark resets ledger baseline on date transition."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from algodesk.engine.backtest import BacktestRunner

    mock_runtime = MagicMock()
    mock_runtime.effective.run = None
    mock_runtime.root = tmp_path
    mock_runtime.paths.results = tmp_path / "results"

    bt = BacktestRunner(runtime=mock_runtime, run_name="bt_baseline_test")
    sr = bt.session_runner

    # Setup simulated context
    ledger = PortfolioLedger(starting_cash=Decimal("1000000.00"))
    c = {"ledger": ledger}
    mock_clock = MagicMock()

    # Session 1: 2025-06-12 at 15:30 IST (Bar event)
    mock_clock.now.return_value = datetime(2025, 6, 12, 10, 0, tzinfo=ZoneInfo("UTC"))
    ledger.starting_cash = Decimal("1445763.28")
    # Call market event for bar
    sr._on_backtest_market_event(c, mock_clock, journal=None, is_bar=True)

    # Session 2: 2025-06-13 at 09:15 IST (Quote event arrives)
    mock_clock.now.return_value = datetime(2025, 6, 13, 3, 45, tzinfo=ZoneInfo("UTC"))
    # Before quote is marked or fills occur, date transition triggers baseline reset
    sr._on_backtest_market_event(c, mock_clock, journal=None, is_bar=False)

    # Baseline for Session 2 must be exactly Session 1 close equity
    assert ledger._start_of_day_equity == Decimal("1445763.28")

    # When opening fills happen and equity changes to 1,435,513.88:
    ledger.starting_cash = Decimal("1435513.88")
    day_pnl = ledger.day_pnl
    assert day_pnl == Decimal("-10249.40")

    # Loss is ₹10,249.40 (~1.02% of capital), well within 4% limit (40,000)
    ks = KillSwitch(state_file=tmp_path / "ks.json")
    monitor = RiskMonitor(
        killswitch=ks,
        portfolio=ledger,
        params={"risk.swing.daily_loss_limit_pct": 4.0},
        profile="swing",
        allocated_capital=Decimal("1000000.00"),
    )
    assert monitor.check_daily_loss() is False
    assert ks.is_active() is False
