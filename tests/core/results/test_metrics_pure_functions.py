"""Hand-computed known-answer tests for pure metrics functions.

Every test documents the underlying arithmetic as comments before assertions.
Edge cases tested: no trades, flat equity, one trade, all winners, all losers,
one-day run, std zero.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from algodesk.results.metrics import (
    compute_cagr,
    compute_daily_returns,
    compute_drawdown_metrics,
    compute_exposure_and_turnover,
    compute_round_trip_performance,
    compute_sharpe,
    compute_sortino,
    compute_total_return,
    compute_volatility,
)


def test_hand_computed_daily_returns() -> None:
    # Equities: [100.0, 105.0, 94.5]
    # r_1 = (105.0 - 100.0) / 100.0 = 5.0 / 100.0 = 0.05
    # r_2 = (94.5 - 105.0) / 105.0 = -10.5 / 105.0 = -0.10
    equities = [Decimal("100.0"), Decimal("105.0"), Decimal("94.5")]
    returns = compute_daily_returns(equities)
    assert len(returns) == 2
    assert math.isclose(returns[0], 0.05, abs_tol=1e-6)
    assert math.isclose(returns[1], -0.10, abs_tol=1e-6)


def test_hand_computed_cagr() -> None:
    # Start: 100,000, End: 121,000, Days: 730.5 (2 years = 2 * 365.25)
    # CAGR = (121000 / 100000) ^ (365.25 / 730.5) - 1
    # CAGR = (1.21) ^ 0.5 - 1 = 1.10 - 1 = 0.10 (10.0%)
    cagr = compute_cagr(Decimal("100000.0"), Decimal("121000.0"), calendar_days=730.5)
    assert math.isclose(cagr, 0.10, abs_tol=1e-5)


def test_hand_computed_volatility() -> None:
    # Returns: [0.01, -0.01, 0.01, -0.01] (n = 4)
    # mean = 0.0
    # squared diffs: 0.0001 + 0.0001 + 0.0001 + 0.0001 = 0.0004
    # sample variance (ddof=1): 0.0004 / (4 - 1) = 0.0004 / 3 = 0.00013333333333333334
    # std = sqrt(0.00013333333333333334) = 0.01154700538
    # Volatility = std * sqrt(252) = 0.01154700538 * 15.874507866387544 = 0.183303
    r = [0.01, -0.01, 0.01, -0.01]
    vol = compute_volatility(r, trading_days_per_year=252)
    expected = math.sqrt(0.0004 / 3.0) * math.sqrt(252)
    assert math.isclose(vol, expected, abs_tol=1e-5)


def test_hand_computed_sharpe() -> None:
    # Returns: [0.02, 0.04] (n = 2)
    # mean = (0.02 + 0.04) / 2 = 0.03
    # squared diffs: (0.02 - 0.03)^2 + (0.04 - 0.03)^2 = 0.0001 + 0.0001 = 0.0002
    # var = 0.0002 / 1 = 0.0002
    # std = sqrt(0.0002) = 0.0141421356
    # risk_free_rate_pct = 5.04% -> daily_rf = (0.0504) / 252 = 0.0002
    # excess return = 0.03 - 0.0002 = 0.0298
    # Sharpe = (0.0298 / 0.0141421356) * sqrt(252)
    # Sharpe = 2.107178 * 15.874508 = 33.4504
    r = [0.02, 0.04]
    sharpe = compute_sharpe(r, risk_free_rate_pct=5.04, trading_days_per_year=252)
    expected = (0.0298 / math.sqrt(0.0002)) * math.sqrt(252)
    assert math.isclose(sharpe, round(expected, 4), abs_tol=1e-4)


def test_hand_computed_sortino() -> None:
    # Returns: [0.05, -0.02, 0.03, -0.01] (n = 4)
    # risk_free_rate_pct = 0.0 -> daily_rf = 0.0
    # mean = (0.05 - 0.02 + 0.03 - 0.01) / 4 = 0.05 / 4 = 0.0125
    # downside diffs: min(0, r - 0) -> [0.0, -0.02, 0.0, -0.01]
    # downside sq: 0.0 + 0.0004 + 0.0 + 0.0001 = 0.0005
    # downside var (ddof=1): 0.0005 / 3 = 0.00016666666666666666
    # downside dev = sqrt(0.0005 / 3) = 0.012909944
    # Sortino = (0.0125 / 0.012909944) * sqrt(252)
    # Sortino = 0.9682458 * 15.8745078 = 15.3704
    r = [0.05, -0.02, 0.03, -0.01]
    sortino = compute_sortino(r, risk_free_rate_pct=0.0, trading_days_per_year=252)
    expected = (0.0125 / math.sqrt(0.0005 / 3.0)) * math.sqrt(252)
    assert math.isclose(sortino, round(expected, 4), abs_tol=1e-4)


def test_hand_computed_drawdown_metrics() -> None:
    # Equities: [100.0, 120.0, 90.0, 110.0, 130.0, 117.0]
    # Peak starts at 100
    # i=1: eq=120 -> new peak=120
    # i=2: eq=90 -> dd = (120 - 90) / 120 = 30 / 120 = 25.0%
    # i=3: eq=110 -> dd = (120 - 110) / 120 = 8.33%
    # i=4: eq=130 -> new peak=130, recovered from peak at i=1; duration = 4 - 1 = 3 sessions
    # i=5: eq=117 -> dd = (130 - 117) / 130 = 10.0%, duration from peak i=4 is 5 - 4 = 1
    # Max DD is 25.0%, longest duration is 3
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    timestamps = [t0 + timedelta(days=i) for i in range(6)]
    equities = [100.0, 120.0, 90.0, 110.0, 130.0, 117.0]
    max_dd, duration = compute_drawdown_metrics(equities, timestamps=timestamps)
    assert max_dd == 25.0
    assert duration == 3


def test_hand_computed_round_trip_performance() -> None:
    # 4 round trips:
    # Net P&Ls: [₹500.0, ₹300.0, -₹200.0, -₹100.0]
    # Win count = 2, Loss count = 2, Total = 4
    # Win rate = 2 / 4 * 100 = 50.0%
    # Total net = 500 + 300 - 200 - 100 = 500.0
    # Expectancy = 500.0 / 4 = ₹125.00
    # Sum wins = 500 + 300 = 800.0
    # Sum losses = |-200| + |-100| = 300.0
    # Profit factor = 800.0 / 300.0 = 2.6667
    rts = [
        {"net_pnl": Decimal("500.00")},
        {"net_pnl": Decimal("300.00")},
        {"net_pnl": Decimal("-200.00")},
        {"net_pnl": Decimal("-100.00")},
    ]
    perf = compute_round_trip_performance(rts)
    assert perf["closed_round_trips"] == 4
    assert perf["win_rate"] == 50.0
    assert perf["expectancy"] == Decimal("125.00")
    assert perf["profit_factor"] == 2.6667


def test_hand_computed_exposure_and_turnover() -> None:
    # 10 sessions: 4 with positions, 6 flat -> exposure = 4 / 10 = 0.40 (40%)
    # Start equity = 100,000, Total traded value = 50,000
    # Turnover = 50000 / 100000 = 0.50
    # Annualized turnover = 0.50 * (252 / 10) = 0.50 * 25.2 = 12.60
    has_pos = [True, True, False, False, True, False, False, True, False, False]
    exposure, turnover = compute_exposure_and_turnover(
        session_has_position=has_pos,
        total_traded_value=Decimal("50000.00"),
        start_equity=Decimal("100000.00"),
        trading_days_per_year=252,
    )
    assert exposure == 0.40
    assert turnover == 12.60


# --- Edge cases required by Card Step 4 ---


def test_edge_case_no_trades() -> None:
    """Edge case: zero trades or round trips."""
    perf = compute_round_trip_performance([])
    assert perf["closed_round_trips"] == 0
    assert perf["win_rate"] == 0.0
    assert perf["expectancy"] == Decimal("0.00")
    assert perf["profit_factor"] is None
    assert perf["winning_round_trips"] == 0
    assert perf["losing_round_trips"] == 0


def test_edge_case_flat_equity() -> None:
    """Edge case: equity remains unchanged throughout the backtest."""
    equities = [Decimal("100000.00")] * 10
    returns = compute_daily_returns(equities)
    assert all(r == 0.0 for r in returns)
    assert compute_total_return(equities[0], equities[-1]) == 0.0
    assert compute_cagr(equities[0], equities[-1], calendar_days=365) == 0.0
    assert compute_volatility(returns) == 0.0
    assert compute_sharpe(returns) == 0.0
    assert compute_sortino(returns) is None
    max_dd, duration = compute_drawdown_metrics(equities)
    assert max_dd == 0.0
    assert duration == 0


def test_edge_case_one_trade() -> None:
    """Edge case: exactly one completed trade."""
    perf = compute_round_trip_performance([{"net_pnl": Decimal("250.00")}])
    assert perf["closed_round_trips"] == 1
    assert perf["win_rate"] == 100.0
    assert perf["expectancy"] == Decimal("250.00")
    assert perf["profit_factor"] is None  # no losses -> None


def test_edge_case_all_winners() -> None:
    """Edge case: 100% win rate (no losses)."""
    rts = [{"net_pnl": Decimal("100.00")}, {"net_pnl": Decimal("200.00")}]
    perf = compute_round_trip_performance(rts)
    assert perf["win_rate"] == 100.0
    assert perf["profit_factor"] is None  # None if no losses
    assert perf["winning_round_trips"] == 2
    assert perf["losing_round_trips"] == 0


def test_edge_case_all_losers() -> None:
    """Edge case: 0% win rate (all losses)."""
    rts = [{"net_pnl": Decimal("-100.00")}, {"net_pnl": Decimal("-50.00")}]
    perf = compute_round_trip_performance(rts)
    assert perf["win_rate"] == 0.0
    assert perf["profit_factor"] == 0.0
    assert perf["winning_round_trips"] == 0
    assert perf["losing_round_trips"] == 2


def test_edge_case_one_day_run() -> None:
    """Edge case: single-day run (0 or 1 equity mark)."""
    equities = [Decimal("100000.00")]
    returns = compute_daily_returns(equities)
    assert returns == []
    assert compute_volatility(returns) == 0.0
    assert compute_sharpe(returns) == 0.0
    assert compute_sortino(returns) is None
    assert compute_cagr(equities[0], equities[0], calendar_days=0) == 0.0


def test_edge_case_std_zero() -> None:
    """Edge case: non-empty daily returns with variance zero."""
    returns = [0.01, 0.01, 0.01, 0.01]  # all identical
    assert compute_volatility(returns) == 0.0
    assert compute_sharpe(returns) == 0.0
    assert compute_sortino(returns) is None
