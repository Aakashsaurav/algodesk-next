"""Pure calculation functions for backtest performance metrics.

Adheres strictly to Decimal money, float ratios, and at most 50-line functions.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any


def compute_daily_returns(daily_equities: Sequence[Decimal | float]) -> list[float]:
    """Calculate percentage daily returns between consecutive daily equity marks."""
    if len(daily_equities) <= 1:
        return []
    returns: list[float] = []
    for prev, curr in zip(daily_equities[:-1], daily_equities[1:], strict=False):
        p_val = float(prev)
        c_val = float(curr)
        if p_val > 0.0:
            returns.append((c_val - p_val) / p_val)
        else:
            returns.append(0.0)
    return returns


def compute_total_return(start_equity: Decimal | float, end_equity: Decimal | float) -> float:
    """Calculate overall total percentage return over the evaluation period."""
    s = float(start_equity)
    e = float(end_equity)
    if s <= 0.0:
        return 0.0
    return round((e - s) / s, 6)


def compute_cagr(
    start_equity: Decimal | float,
    end_equity: Decimal | float,
    calendar_days: int | float,
) -> float:
    """Calculate Compound Annual Growth Rate = (end/start)^(365.25/days) - 1."""
    s = float(start_equity)
    e = float(end_equity)
    days = float(calendar_days)
    if s <= 0.0 or e <= 0.0 or days <= 0.0:
        return 0.0
    try:
        cagr = (e / s) ** (365.25 / days) - 1.0
        return round(float(cagr), 6)
    except (ZeroDivisionError, OverflowError, ValueError):
        return 0.0


def compute_volatility(
    daily_returns: Sequence[float],
    trading_days_per_year: int = 252,
) -> float:
    """Calculate annualized volatility = std(daily return, ddof=1) * sqrt(trading_days)."""
    n = len(daily_returns)
    if n < 2:
        return 0.0
    mean_r = sum(daily_returns) / n
    variance = sum((r - mean_r) ** 2 for r in daily_returns) / (n - 1)
    std = math.sqrt(max(0.0, variance))
    if std == 0.0:
        return 0.0
    vol = std * math.sqrt(trading_days_per_year)
    return round(vol, 6)


def compute_sharpe(
    daily_returns: Sequence[float],
    risk_free_rate_pct: float = 0.0,
    trading_days_per_year: int = 252,
) -> float:
    """Calculate annualized Sharpe ratio = (mean_r - daily_rf) / std * sqrt(trading_days)."""
    n = len(daily_returns)
    if n < 2:
        return 0.0
    daily_rf = (risk_free_rate_pct / 100.0) / trading_days_per_year
    mean_r = sum(daily_returns) / n
    variance = sum((r - mean_r) ** 2 for r in daily_returns) / (n - 1)
    std = math.sqrt(max(0.0, variance))
    if std == 0.0:
        return 0.0
    sharpe = ((mean_r - daily_rf) / std) * math.sqrt(trading_days_per_year)
    return round(sharpe, 4)


def compute_sortino(
    daily_returns: Sequence[float],
    risk_free_rate_pct: float = 0.0,
    trading_days_per_year: int = 252,
) -> float:
    """Calculate annualized Sortino ratio with downside deviation below the risk-free rate."""
    n = len(daily_returns)
    if n < 2:
        return 0.0
    daily_rf = (risk_free_rate_pct / 100.0) / trading_days_per_year
    mean_r = sum(daily_returns) / n
    downside_sq = [min(0.0, r - daily_rf) ** 2 for r in daily_returns]
    downside_var = sum(downside_sq) / (n - 1)
    downside_dev = math.sqrt(max(0.0, downside_var))
    if downside_dev == 0.0:
        return 0.0
    sortino = ((mean_r - daily_rf) / downside_dev) * math.sqrt(trading_days_per_year)
    return round(sortino, 4)


def compute_drawdown_metrics(
    equity_series: Sequence[Decimal | float],
    timestamps: Sequence[datetime] | None = None,
) -> tuple[float, int]:
    """Calculate maximum drawdown (%) and longest drawdown duration in calendar days.

    Duration measures peak to full recovery, or to the end of the series if not recovered.
    """
    if not equity_series:
        return 0.0, 0
    equities = [float(x) for x in equity_series]
    max_dd_pct = 0.0
    peak = equities[0]
    peak_idx = 0
    in_drawdown = False
    max_duration_days = 0

    for i, eq in enumerate(equities):
        if eq > peak:
            if in_drawdown:
                duration = _measure_duration(timestamps, peak_idx, i)
                if duration > max_duration_days:
                    max_duration_days = duration
                in_drawdown = False
            peak = eq
            peak_idx = i
        elif eq == peak:
            if in_drawdown:
                duration = _measure_duration(timestamps, peak_idx, i)
                if duration > max_duration_days:
                    max_duration_days = duration
                in_drawdown = False
            peak_idx = i
        else:
            in_drawdown = True
            dd = (peak - eq) / peak * 100.0
            if dd > max_dd_pct:
                max_dd_pct = dd
            duration = _measure_duration(timestamps, peak_idx, i)
            if duration > max_duration_days:
                max_duration_days = duration

    return round(max_dd_pct, 4), max_duration_days


def _measure_duration(
    timestamps: Sequence[datetime] | None,
    start_idx: int,
    end_idx: int,
) -> int:
    """Compute duration between indices in days if timestamps available, else session count."""
    if timestamps and len(timestamps) > end_idx:
        dt0 = timestamps[start_idx]
        dt1 = timestamps[end_idx]
        return max(0, (dt1.date() - dt0.date()).days)
    return max(0, end_idx - start_idx)


def compute_round_trip_performance(
    round_trips: Sequence[Any],
) -> dict[str, Any]:
    """Calculate win rate, expectancy, and profit factor per closed round trip net of charges."""
    closed_count = len(round_trips)
    if closed_count == 0:
        return {
            "closed_round_trips": 0,
            "win_rate": 0.0,
            "expectancy": Decimal("0.00"),
            "profit_factor": None,
            "winning_round_trips": 0,
            "losing_round_trips": 0,
        }

    net_pnls = [
        Decimal(
            str(getattr(rt, "net_pnl", 0.0) if hasattr(rt, "net_pnl") else rt.get("net_pnl", 0))
        )
        for rt in round_trips
    ]
    wins = [p for p in net_pnls if p > Decimal("0.00")]
    losses = [p for p in net_pnls if p < Decimal("0.00")]

    win_count = len(wins)
    loss_count = len(losses)
    win_rate = round(float(win_count / closed_count * 100.0), 2)
    total_net = sum(net_pnls, Decimal("0.00"))
    expectancy = round(total_net / Decimal(str(closed_count)), 2)

    sum_wins = sum(wins, Decimal("0.00"))
    sum_losses = sum((abs(loss) for loss in losses), Decimal("0.00"))

    if sum_losses == Decimal("0.00"):
        profit_factor = None
    else:
        profit_factor = round(float(sum_wins / sum_losses), 4)

    return {
        "closed_round_trips": closed_count,
        "win_rate": win_rate,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "winning_round_trips": win_count,
        "losing_round_trips": loss_count,
    }


def compute_exposure_and_turnover(
    session_has_position: Sequence[bool],
    total_traded_value: Decimal | float,
    start_equity: Decimal | float,
    trading_days_per_year: int = 252,
) -> tuple[float, float]:
    """Calculate portfolio exposure share and annualized portfolio turnover."""
    total_sessions = len(session_has_position)
    if total_sessions == 0:
        return 0.0, 0.0
    pos_count = sum(1 for has_pos in session_has_position if has_pos)
    exposure = round(float(pos_count / total_sessions), 4)

    s_eq = float(start_equity)
    traded_val = float(total_traded_value)
    if s_eq <= 0.0:
        return exposure, 0.0
    turnover = traded_val / s_eq
    annualized_turnover = round(turnover * (trading_days_per_year / max(1, total_sessions)), 4)
    return exposure, annualized_turnover
