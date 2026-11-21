"""Unit and conformance tests for screen_rotation strategy plugin."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from algodesk.api import (
    InstrumentId,
    PlaceOrder,
    PluginManifest,
    Product,
    Side,
)
from algodesk.api.conformance import strategy_problems
from algodesk.api.market import Bar
from algodesk.api.protocols import ScreenSelection, StrategyContext
from algodesk.api.trading import Fill, Position

from ..plugin import ScreenRotationStrategy


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


class DummyPortfolio:
    def __init__(self, equity: float = 100000.0) -> None:
        self.equity = Decimal(str(equity))
        self.cash = Decimal(str(equity))


def _make_manifest() -> PluginManifest:
    return PluginManifest(
        name="screen_rotation",
        version="0.1.0",
        kind="strategy",  # type: ignore[arg-type]
        api_version=1,
        entry="plugin:ScreenRotationStrategy",
        capabilities={"backtest": True},
    )


def test_strategy_conformance() -> None:
    manifest = _make_manifest()
    strat = ScreenRotationStrategy(manifest, {"screen": "trend_rank_test"})
    problems = strategy_problems(strat)
    assert not problems, f"Conformance problems: {problems}"


def test_no_forbidden_imports() -> None:
    import inspect

    from .. import plugin as mod

    source = inspect.getsource(mod)
    assert "algodesk.screens" not in source
    assert "algodesk.data" not in source
    assert "algodesk.venues" not in source
    assert "algodesk.brokers" not in source


def test_rebalance_trigger_and_deterministic_ranking() -> None:
    manifest = _make_manifest()
    strat = ScreenRotationStrategy(
        manifest,
        {"screen": "test_scr", "top_n": 3, "weight_pct": 15.0, "rebalance_every": 21},
    )

    # Mock screen returning symbols with ties
    mock_selection = ScreenSelection(
        symbols=("NSE:EQ:B", "NSE:EQ:A", "NSE:EQ:C", "NSE:EQ:D"),
        scores={"NSE:EQ:A": 80.0, "NSE:EQ:B": 80.0, "NSE:EQ:C": 90.0, "NSE:EQ:D": 50.0},
        as_of=date(2024, 1, 2),
        result_hash="hash123",
    )

    positions: dict[str, Position] = {}

    ctx = StrategyContext(
        screen=lambda name, *, group=None: mock_selection,
        bars=lambda inst, tf, n: (
            _make_bar(inst, datetime(2024, 1, 2, 10, 0, tzinfo=UTC), close=100.0),
        ),
        position=lambda inst: positions.get(str(inst)),
        portfolio=DummyPortfolio(100000.0),
    )

    bar1 = _make_bar("NSE:EQ:A", datetime(2024, 1, 2, 10, 0, tzinfo=UTC), close=100.0)
    intents = strat.on_bar(ctx, bar1)

    # Ranking: C (90.0), then A (80.0), then B (80.0) - tie break alphabetical, top 3 = C, A, B
    assert len(intents) == 3
    bought = [intent.spec.instrument for intent in intents if isinstance(intent, PlaceOrder)]
    assert bought == [InstrumentId("NSE:EQ:C"), InstrumentId("NSE:EQ:A"), InstrumentId("NSE:EQ:B")]

    # Weight 15% of 100,000 = 15,000 / 100.0 = 150 qty
    for intent in intents:
        assert isinstance(intent, PlaceOrder)
        assert intent.spec.qty == 150
        assert intent.spec.side == Side.BUY

    # Rebalance entry recorded
    assert len(strat.rebalances) == 1
    assert strat.rebalances[0]["rebalance_date"] == "2024-01-02"
    assert strat.rebalances[0]["screen_result_hash"] == "hash123"


def test_no_action_between_rebalances() -> None:
    manifest = _make_manifest()
    strat = ScreenRotationStrategy(
        manifest,
        {"screen": "test_scr", "top_n": 2, "weight_pct": 10.0, "rebalance_every": 5},
    )

    mock_selection = ScreenSelection(
        symbols=("NSE:EQ:A", "NSE:EQ:B"),
        scores={"NSE:EQ:A": 90.0, "NSE:EQ:B": 80.0},
        as_of=date(2024, 1, 2),
        result_hash="h1",
    )
    ctx = StrategyContext(
        screen=lambda name, *, group=None: mock_selection,
        bars=lambda inst, tf, n: (
            _make_bar(inst, datetime(2024, 1, 2, 10, 0, tzinfo=UTC), close=100.0),
        ),
        position=lambda inst: None,
        portfolio=DummyPortfolio(100000.0),
    )

    # Session 1: rebalances
    intents1 = strat.on_bar(ctx, _make_bar("NSE:EQ:A", datetime(2024, 1, 2, 10, 0, tzinfo=UTC)))
    assert len(intents1) == 2

    # Session 2, 3, 4: no action
    for d in (3, 4, 5):
        intents_mid = strat.on_bar(
            ctx, _make_bar("NSE:EQ:A", datetime(2024, 1, d, 10, 0, tzinfo=UTC))
        )
        assert intents_mid == ()

    # Session 5: no action (because rebalance_every=5, next is 1 + 5 = 6)
    intents5 = strat.on_bar(ctx, _make_bar("NSE:EQ:A", datetime(2024, 1, 6, 10, 0, tzinfo=UTC)))
    assert intents5 == ()

    # Session 6: rebalances!
    strat.on_bar(ctx, _make_bar("NSE:EQ:A", datetime(2024, 1, 7, 10, 0, tzinfo=UTC)))
    assert len(strat.rebalances) == 2


def test_sell_departing_positions_and_keep_held() -> None:
    manifest = _make_manifest()
    strat = ScreenRotationStrategy(
        manifest,
        {"screen": "test_scr", "top_n": 2, "weight_pct": 10.0, "rebalance_every": 2},
    )

    selection1 = ScreenSelection(
        symbols=("NSE:EQ:A", "NSE:EQ:B"),
        scores={"NSE:EQ:A": 90.0, "NSE:EQ:B": 80.0},
        as_of=date(2024, 1, 2),
        result_hash="h1",
    )
    positions: dict[str, Position] = {}
    ctx = StrategyContext(
        screen=lambda name, *, group=None: selection1,
        bars=lambda inst, tf, n: (
            _make_bar(inst, datetime(2024, 1, 2, 10, 0, tzinfo=UTC), close=100.0),
        ),
        position=lambda inst: positions.get(str(inst)),
        portfolio=DummyPortfolio(100000.0),
    )

    strat.on_bar(ctx, _make_bar("NSE:EQ:A", datetime(2024, 1, 2, 10, 0, tzinfo=UTC)))
    # Suppose both filled
    positions["NSE:EQ:A"] = Position(
        instrument=InstrumentId("NSE:EQ:A"),
        product=Product.CNC,
        qty=100,
        avg_price=Decimal("100"),
    )
    positions["NSE:EQ:B"] = Position(
        instrument=InstrumentId("NSE:EQ:B"),
        product=Product.CNC,
        qty=100,
        avg_price=Decimal("100"),
    )
    strat.on_fill(
        ctx,
        Fill(
            fill_id="f1",
            client_ref="c1",
            instrument=InstrumentId("NSE:EQ:A"),
            side=Side.BUY,
            qty=100,
            price=Decimal("100"),
            ts=datetime(2024, 1, 2, 10, 0, tzinfo=UTC),
        ),
    )
    strat.on_fill(
        ctx,
        Fill(
            fill_id="f2",
            client_ref="c2",
            instrument=InstrumentId("NSE:EQ:B"),
            side=Side.BUY,
            qty=100,
            price=Decimal("100"),
            ts=datetime(2024, 1, 2, 10, 0, tzinfo=UTC),
        ),
    )

    # Session 2: no rebalance
    strat.on_bar(ctx, _make_bar("NSE:EQ:A", datetime(2024, 1, 3, 10, 0, tzinfo=UTC)))

    # Session 3: selection changes -> A and C (B drops out!)
    selection2 = ScreenSelection(
        symbols=("NSE:EQ:A", "NSE:EQ:C"),
        scores={"NSE:EQ:A": 95.0, "NSE:EQ:C": 85.0},
        as_of=date(2024, 1, 4),
        result_hash="h2",
    )
    ctx2 = StrategyContext(
        screen=lambda name, *, group=None: selection2,
        bars=lambda inst, tf, n: (
            _make_bar(inst, datetime(2024, 1, 4, 10, 0, tzinfo=UTC), close=100.0),
        ),
        position=lambda inst: positions.get(str(inst)),
        portfolio=DummyPortfolio(100000.0),
    )
    intents3 = strat.on_bar(ctx2, _make_bar("NSE:EQ:A", datetime(2024, 1, 4, 10, 0, tzinfo=UTC)))

    # Step 1: SELL B emitted at rebalance bar, replacement BUY deferred until sell fills
    sells = [i for i in intents3 if isinstance(i, PlaceOrder) and i.spec.side == Side.SELL]
    buys = [i for i in intents3 if isinstance(i, PlaceOrder) and i.spec.side == Side.BUY]

    assert len(sells) == 1
    assert sells[0].spec.instrument == InstrumentId("NSE:EQ:B")
    assert sells[0].spec.qty == 100
    assert len(buys) == 0

    # Fill the SELL of B
    del positions["NSE:EQ:B"]
    strat.on_fill(
        ctx2,
        Fill(
            fill_id="f3",
            client_ref="c3",
            instrument=InstrumentId("NSE:EQ:B"),
            side=Side.SELL,
            qty=100,
            price=Decimal("100"),
            ts=datetime(2024, 1, 4, 10, 5, tzinfo=UTC),
        ),
    )

    # Step 2: Next completed bar executes deferred replacement BUY of C
    intents4 = strat.on_bar(ctx2, _make_bar("NSE:EQ:A", datetime(2024, 1, 4, 10, 10, tzinfo=UTC)))
    buys_step2 = [i for i in intents4 if isinstance(i, PlaceOrder) and i.spec.side == Side.BUY]
    assert len(buys_step2) == 1
    assert buys_step2[0].spec.instrument == InstrumentId("NSE:EQ:C")
    assert buys_step2[0].spec.qty == 100


def test_state_serialization_and_restore() -> None:
    manifest = _make_manifest()
    strat = ScreenRotationStrategy(manifest, {"screen": "test_scr"})
    strat._sessions_seen = 42
    strat._rebalance_count = 3
    strat._last_selection = ("A", "B", "C")
    strat._current_target = ("A", "B")
    strat._last_session_date = date(2024, 5, 10)

    st = strat.state()
    assert st["sessions_seen"] == 42
    assert st["rebalance_count"] == 3
    assert st["last_selection"] == ["A", "B", "C"]
    assert st["current_target"] == ["A", "B"]
    assert st["last_session_date"] == "2024-05-10"

    strat2 = ScreenRotationStrategy(manifest, {"screen": "test_scr"})
    strat2.restore(st)
    assert strat2._sessions_seen == 42
    assert strat2._rebalance_count == 3
    assert strat2._last_selection == ("A", "B", "C")
    assert strat2._current_target == ("A", "B")
    assert strat2._last_session_date == date(2024, 5, 10)
