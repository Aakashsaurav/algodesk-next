"""Comprehensive tests for multiple technical indicator outputs and deep crossover windows."""

from __future__ import annotations

from tests.core.screens.conftest import FakeIndicatorResolver, generate_daily_bars

from algodesk.screens.features import build_feature_view
from algodesk.screens.models import (
    OperandModel,
    RequireRule,
    TomlScreenModel,
)
from algodesk.screens.rules import eval_screen_rules


def _make_view(symbol: str, prices: list[float], resolver: FakeIndicatorResolver):
    bars = generate_daily_bars(symbol, prices)
    return build_feature_view(
        symbols=[symbol],
        bars_by_symbol={symbol: bars},
        indicator_resolver=resolver,
    ).for_symbol(symbol)


def test_bollinger_bands_within_channel(fake_resolver: FakeIndicatorResolver) -> None:
    # 30 bars around 100
    prices = [100.0 + (i % 3) for i in range(30)]
    view = _make_view("SYM1", prices, fake_resolver)
    # close should be > lower and < upper
    r1 = RequireRule(
        left=OperandModel(field="close"),
        op=">",
        right=OperandModel(ind="bollinger", n=20, output="lower"),
    )
    r2 = RequireRule(
        left=OperandModel(field="close"),
        op="<",
        right=OperandModel(ind="bollinger", n=20, output="upper"),
    )
    screen = TomlScreenModel(name="bb_test", require=[r1, r2])
    row = eval_screen_rules(screen, view)
    assert row.passed is True
    assert row.values["bollinger_20_lower"] is not None
    assert row.values["bollinger_20_upper"] is not None


def test_macd_line_above_signal(fake_resolver: FakeIndicatorResolver) -> None:
    # Accelerating price growth so MACD line > MACD signal
    prices = [50.0 + (i * i * 0.05) for i in range(60)]
    view = _make_view("SYM1", prices, fake_resolver)
    rule = RequireRule(
        left=OperandModel(ind="macd", fast=12, slow=26, signal=9, output="macd"),
        op=">",
        right=OperandModel(ind="macd", fast=12, slow=26, signal=9, output="signal"),
    )
    screen = TomlScreenModel(name="macd_test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True
    assert row.values["macd_12_9_26_macd"] > row.values["macd_12_9_26_signal"]


def test_macd_histogram_positive(fake_resolver: FakeIndicatorResolver) -> None:
    prices = [50.0 + (i * i * 0.05) for i in range(60)]
    view = _make_view("SYM1", prices, fake_resolver)
    rule = RequireRule(
        left=OperandModel(ind="macd", fast=12, slow=26, signal=9, output="histogram"),
        op=">",
        right=OperandModel(const=0.0),
    )
    screen = TomlScreenModel(name="macd_hist", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True
    assert row.values["macd_12_9_26_histogram"] > 0.0


def test_rolling_hl_high(fake_resolver: FakeIndicatorResolver) -> None:
    # 25 bars: peak was at index 20 (price 150), last bar is 140
    prices = [100.0 + i for i in range(20)] + [150.0, 145.0, 142.0, 141.0, 140.0]
    view = _make_view("SYM1", prices, fake_resolver)
    rule = RequireRule(
        left=OperandModel(ind="rolling_hl", n=10, output="high"),
        op=">=",
        right=OperandModel(field="close"),
    )
    screen = TomlScreenModel(name="hl_test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True
    # Highest high of last 10 bars is 150 * 1.02
    assert row.values["rolling_hl_10_high"] >= 150.0


def test_rate_of_change_positive(fake_resolver: FakeIndicatorResolver) -> None:
    prices = [100.0 + (i * 2.0) for i in range(25)]
    view = _make_view("SYM1", prices, fake_resolver)
    rule = RequireRule(
        left=OperandModel(ind="roc", n=12),
        op=">",
        right=OperandModel(const=0.0),
    )
    screen = TomlScreenModel(name="roc_test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True
    assert row.values["roc_12"] > 0.0


def test_atr_positive(fake_resolver: FakeIndicatorResolver) -> None:
    prices = [100.0 + (i * 1.5) for i in range(30)]
    view = _make_view("SYM1", prices, fake_resolver)
    rule = RequireRule(
        left=OperandModel(ind="atr", n=14, output="atr"),
        op=">",
        right=OperandModel(const=0.0),
    )
    screen = TomlScreenModel(name="atr_test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True
    assert row.values["atr_14_atr"] > 0.0


def test_volume_avg_comparison(fake_resolver: FakeIndicatorResolver) -> None:
    bars = generate_daily_bars("SYM1", [100.0] * 25, volume=5000.0)
    view = build_feature_view(
        symbols=["SYM1"],
        bars_by_symbol={"SYM1": bars},
        indicator_resolver=fake_resolver,
    ).for_symbol("SYM1")
    rule = RequireRule(
        left=OperandModel(field="volume"),
        op=">=",
        right=OperandModel(ind="volume_avg", n=10, output="volume_avg"),
    )
    screen = TomlScreenModel(name="v_avg_test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_crosses_above_within_5_bars(fake_resolver: FakeIndicatorResolver) -> None:
    # 7 bars: crossover occurred 4 bars ago (offset 3)
    # Prices: [40, 48, 55, 56, 57, 58, 59]
    # At offset 5 (price 48): <= 50
    # At offset 4 (price 55): > 50 -> crossover happened at offset 4 (within 5)
    prices = [40.0, 48.0, 55.0, 56.0, 57.0, 58.0, 59.0]
    view = _make_view("SYM1", prices, fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="crosses_above",
        right=OperandModel(const=50.0),
        within=5,
    )
    screen = TomlScreenModel(name="x_test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True
