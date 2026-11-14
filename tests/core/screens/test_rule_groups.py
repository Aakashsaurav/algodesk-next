"""Tests for composite rule groups (any, not, at_least)."""

from __future__ import annotations

from tests.core.screens.conftest import FakeIndicatorResolver, generate_daily_bars

from algodesk.screens.features import build_feature_view
from algodesk.screens.models import (
    GroupRule,
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


def test_group_any_passes_when_one_holds(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    r1 = RequireRule(
        left=OperandModel(field="close"), op=">", right=OperandModel(const=40.0)
    )  # True
    r2 = RequireRule(
        left=OperandModel(field="close"), op="<", right=OperandModel(const=20.0)
    )  # False
    group = GroupRule(mode="any", require=[r1, r2])
    screen = TomlScreenModel(name="test", group=[group])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_group_any_passes_when_all_hold(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    r1 = RequireRule(
        left=OperandModel(field="close"), op=">", right=OperandModel(const=30.0)
    )  # True
    r2 = RequireRule(
        left=OperandModel(field="open"), op=">", right=OperandModel(const=30.0)
    )  # True
    group = GroupRule(mode="any", require=[r1, r2])
    screen = TomlScreenModel(name="test", group=[group])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_group_any_fails_when_none_hold(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    r1 = RequireRule(
        left=OperandModel(field="close"), op=">", right=OperandModel(const=100.0)
    )  # False
    r2 = RequireRule(
        left=OperandModel(field="open"), op="<", right=OperandModel(const=10.0)
    )  # False
    group = GroupRule(mode="any", require=[r1, r2])
    screen = TomlScreenModel(name="test", group=[group])
    row = eval_screen_rules(screen, view)
    assert row.passed is False


def test_group_not_passes_when_none_hold(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    # Neither condition holds -> NOT(any) is True
    r1 = RequireRule(
        left=OperandModel(field="close"), op="<", right=OperandModel(const=20.0)
    )  # False
    r2 = RequireRule(
        left=OperandModel(field="open"), op=">", right=OperandModel(const=100.0)
    )  # False
    group = GroupRule(mode="not", require=[r1, r2])
    screen = TomlScreenModel(name="test", group=[group])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_group_not_fails_when_one_holds(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    r1 = RequireRule(
        left=OperandModel(field="close"), op=">", right=OperandModel(const=40.0)
    )  # True
    r2 = RequireRule(
        left=OperandModel(field="open"), op=">", right=OperandModel(const=100.0)
    )  # False
    group = GroupRule(mode="not", require=[r1, r2])
    screen = TomlScreenModel(name="test", group=[group])
    row = eval_screen_rules(screen, view)
    assert row.passed is False


def test_group_not_fails_when_all_hold(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    r1 = RequireRule(
        left=OperandModel(field="close"), op=">", right=OperandModel(const=30.0)
    )  # True
    r2 = RequireRule(
        left=OperandModel(field="open"), op=">", right=OperandModel(const=30.0)
    )  # True
    group = GroupRule(mode="not", require=[r1, r2])
    screen = TomlScreenModel(name="test", group=[group])
    row = eval_screen_rules(screen, view)
    assert row.passed is False


def test_group_at_least_passes_when_exact_count(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    r1 = RequireRule(
        left=OperandModel(field="close"), op=">", right=OperandModel(const=40.0)
    )  # True
    r2 = RequireRule(
        left=OperandModel(field="open"), op=">", right=OperandModel(const=40.0)
    )  # True
    r3 = RequireRule(
        left=OperandModel(field="close"), op="<", right=OperandModel(const=20.0)
    )  # False
    group = GroupRule(mode="at_least", n=2, require=[r1, r2, r3])
    screen = TomlScreenModel(name="test", group=[group])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_group_at_least_passes_when_more_than_n(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    r1 = RequireRule(
        left=OperandModel(field="close"), op=">", right=OperandModel(const=40.0)
    )  # True
    r2 = RequireRule(
        left=OperandModel(field="open"), op=">", right=OperandModel(const=40.0)
    )  # True
    r3 = RequireRule(
        left=OperandModel(field="high"), op=">", right=OperandModel(const=40.0)
    )  # True
    group = GroupRule(mode="at_least", n=2, require=[r1, r2, r3])
    screen = TomlScreenModel(name="test", group=[group])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_group_at_least_fails_when_less_than_n(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    r1 = RequireRule(
        left=OperandModel(field="close"), op=">", right=OperandModel(const=40.0)
    )  # True
    r2 = RequireRule(
        left=OperandModel(field="open"), op="<", right=OperandModel(const=20.0)
    )  # False
    r3 = RequireRule(
        left=OperandModel(field="close"), op="<", right=OperandModel(const=20.0)
    )  # False
    group = GroupRule(mode="at_least", n=2, require=[r1, r2, r3])
    screen = TomlScreenModel(name="test", group=[group])
    row = eval_screen_rules(screen, view)
    assert row.passed is False


def test_mixed_require_and_groups(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    req = RequireRule(
        left=OperandModel(field="close"), op=">", right=OperandModel(const=40.0)
    )  # True
    g1 = GroupRule(
        mode="any",
        require=[
            RequireRule(
                left=OperandModel(field="open"), op=">", right=OperandModel(const=100.0)
            ),  # False
            RequireRule(
                left=OperandModel(field="volume"), op=">=", right=OperandModel(const=500.0)
            ),  # True
        ],
    )
    g2 = GroupRule(
        mode="not",
        require=[
            RequireRule(
                left=OperandModel(field="low"), op="<", right=OperandModel(const=10.0)
            ),  # False
        ],
    )
    screen = TomlScreenModel(name="test", require=[req], group=[g1, g2])
    row = eval_screen_rules(screen, view)
    assert row.passed is True


def test_multiple_groups_one_fails_means_not_passed(fake_resolver: FakeIndicatorResolver) -> None:
    view = _make_view("SYM1", [10, 20, 30, 40, 50], fake_resolver)
    g1 = GroupRule(
        mode="any",
        require=[
            RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=40.0))
        ],  # True
    )
    g2 = GroupRule(
        mode="any",
        require=[
            RequireRule(left=OperandModel(field="close"), op="<", right=OperandModel(const=10.0))
        ],  # False
    )
    screen = TomlScreenModel(name="test", group=[g1, g2])
    row = eval_screen_rules(screen, view)
    assert row.passed is False
