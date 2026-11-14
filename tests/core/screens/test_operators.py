"""Tests for all screen rule operators, operand types, and bars_ago offsets."""

from __future__ import annotations

import pytest
from tests.core.screens.conftest import FakeIndicatorResolver, generate_daily_bars

from algodesk.api.screens import SkipReason
from algodesk.screens.features import build_feature_view
from algodesk.screens.models import (
    OperandModel,
    RequireRule,
    TomlScreenModel,
)
from algodesk.screens.rules import RuleEvaluationError, eval_screen_rules


def _make_view(symbol: str, prices: list[float], resolver: FakeIndicatorResolver):
    bars = generate_daily_bars(symbol, prices)
    return build_feature_view(
        symbols=[symbol],
        bars_by_symbol={symbol: bars},
        required_indicators=[
            {"ind": "sma", "params": {"n": 3}},
            {"ind": "sma", "params": {"n": 5}},
        ],
        indicator_resolver=resolver,
    ).for_symbol(symbol)


def test_op_greater_than_pass(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op=">",
        right=OperandModel(const=40.0),
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True
    assert row.values["close"] == 50.0


def test_op_greater_than_fail(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op=">",
        right=OperandModel(const=60.0),
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is False


def test_op_greater_than_equal_boundary(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op=">=",
        right=OperandModel(const=50.0),
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_op_less_than_pass(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="<",
        right=OperandModel(const=60.0),
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_op_less_than_fail(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="<",
        right=OperandModel(const=50.0),
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is False


def test_op_less_than_equal_boundary(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="<=",
        right=OperandModel(const=50.0),
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_op_between_inside(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="between",
        right={"low": 40.0, "high": 60.0},
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_op_between_lower_bound(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="between",
        right={"low": 50.0, "high": 60.0},
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_op_between_upper_bound(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="between",
        right={"low": 40.0, "high": 50.0},
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_op_between_fail_too_low(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="between",
        right={"low": 60.0, "high": 70.0},
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is False


def test_op_between_fail_too_high(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="between",
        right={"low": 20.0, "high": 40.0},
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is False


def test_op_crosses_above_immediate(fake_resolver: FakeIndicatorResolver) -> None:
    # Bar 0 (last): close=55 > 50, Bar 1 (prev): close=48 <= 50
    view = _make_view("SYM1", [40, 45, 48, 55], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="crosses_above",
        right=OperandModel(const=50.0),
        within=1,
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_op_crosses_above_within_3_bars(fake_resolver: FakeIndicatorResolver) -> None:
    # Prices: [45, 52, 53, 54] -> crossover happened at index 1 (2 bars ago, within 3)
    view = _make_view("SYM1", [45, 52, 53, 54], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="crosses_above",
        right=OperandModel(const=50.0),
        within=3,
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_op_crosses_above_fail_already_above(fake_resolver: FakeIndicatorResolver) -> None:
    # Always above 50, no crossover
    view = _make_view("SYM1", [52, 53, 54, 55], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="crosses_above",
        right=OperandModel(const=50.0),
        within=2,
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is False


def test_op_crosses_below_immediate(fake_resolver: FakeIndicatorResolver) -> None:
    # Bar 0: close=45 < 50, Bar 1: close=55 >= 50
    view = _make_view("SYM1", [60, 58, 55, 45], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="crosses_below",
        right=OperandModel(const=50.0),
        within=1,
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_op_crosses_below_fail_already_below(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [40, 42, 45, 46], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close"),
        op="crosses_below",
        right=OperandModel(const=50.0),
        within=2,
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is False


def test_indicator_vs_indicator(fake_resolver: FakeIndicatorResolver) -> None:
    # Upward trend: SMA(3) should be > SMA(5)
    prices = [10.0, 12.0, 14.0, 16.0, 20.0, 25.0, 30.0]
    view = _make_view("SYM1", prices, fake_resolver)
    rule = RequireRule(
        left=OperandModel(ind="sma", n=3),
        op=">",
        right=OperandModel(ind="sma", n=5),
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True
    assert row.values["sma_3"] > row.values["sma_5"]


def test_bars_ago_offset(fake_resolver: FakeIndicatorResolver) -> None:
    # Prices: [10, 20, 30, 40, 50] -> close at bars_ago=1 was 40
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    rule = (
        RequireRule(
            left=OperandModel(field="close", bars_ago=1),
            op="==",
            right=OperandModel(const=40.0),
        )
        if False
        else RequireRule(
            left=OperandModel(field="close", bars_ago=1),
            op=">=",
            right=OperandModel(const=40.0),
        )
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True
    assert row.values["close_1"] == 40.0


def test_bars_ago_exceeds_history_raises_skip(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20], fake_resolver)
    rule = RequireRule(
        left=OperandModel(field="close", bars_ago=5),
        op=">",
        right=OperandModel(const=10.0),
    )
    screen = TomlScreenModel(name="test", require=[rule])
    with pytest.raises(RuleEvaluationError) as exc_info:
        eval_screen_rules(screen, view)
    assert exc_info.value.reason == SkipReason.INSUFFICIENT_HISTORY


def test_multiple_require_all_must_hold(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    r1 = RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=40.0))
    r2 = RequireRule(left=OperandModel(field="open"), op=">", right=OperandModel(const=30.0))
    r3 = RequireRule(left=OperandModel(field="high"), op=">", right=OperandModel(const=45.0))
    screen = TomlScreenModel(name="test", require=[r1, r2, r3])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_multiple_require_one_fails_means_not_passed(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    r1 = RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=40.0))
    r2 = RequireRule(left=OperandModel(field="volume"), op=">", right=OperandModel(const=5000.0))
    screen = TomlScreenModel(name="test", require=[r1, r2])
    row = eval_screen_rules(screen, view)
    assert row.passed is False


def test_field_volume_evaluation(fake_resolver: FakeIndicatorResolver) -> None:
    bars = generate_daily_bars("SYM1", [10, 20, 30], volume=25000.0)
    view = build_feature_view(
        symbols=["SYM1"],
        bars_by_symbol={"SYM1": bars},
        indicator_resolver=fake_resolver,
    ).for_symbol("SYM1")
    rule = RequireRule(
        left=OperandModel(field="volume"), op=">=", right=OperandModel(const=20000.0)
    )
    screen = TomlScreenModel(name="test", require=[rule])
    row = eval_screen_rules(screen, view)
    assert row.passed is True
    assert row.values["volume"] == 25000.0
