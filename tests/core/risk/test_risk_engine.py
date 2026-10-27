"""Tests for RiskEngine, KillSwitch, RiskMonitor, and pre-trade risk evaluation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from algodesk.api.market import Instrument, InstrumentId, Level, Quote
from algodesk.api.trading import (
    Charges,
    Fill,
    OrderSpec,
    OrderType,
    PlaceOrder,
    Position,
    Product,
    RiskDecision,
    Side,
)
from algodesk.cli.app import app
from algodesk.engine.events import FillEvent
from algodesk.risk.engine import RiskEngine, is_reducing_intent
from algodesk.risk.killswitch import KillSwitch
from algodesk.risk.monitor import RiskMonitor
from algodesk.risk.view import RiskViewContext


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 27, 9, 30, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def monotonic_ns(self) -> int:
        return int(self._now.timestamp() * 1_000_000_000)


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, kind: str, payload: dict[str, Any], *, durable: bool = False) -> int:
        self.events.append({"kind": kind, "payload": payload, "durable": durable})
        return len(self.events)


class FakeRule:
    def __init__(self, name: str, allow: bool = True, values: dict[str, Any] | None = None) -> None:
        self.name = name
        self.allow = allow
        self.values = values or {}
        self.called_with: list[tuple[Any, Any]] = []

    def check(self, intent: Any, view: Any) -> RiskDecision:
        self.called_with.append((intent, view))
        return RiskDecision(
            allow=self.allow,
            rule=self.name,
            reason=f"Rule {self.name} allow={self.allow}",
            values=self.values,
        )


def make_quote(
    instrument: str = "NSE:EQ:INFY",
    ltp: float = 100.0,
    recv_ts: datetime | None = None,
) -> Quote:
    ts = recv_ts or datetime(2026, 10, 27, 9, 30, tzinfo=UTC)
    return Quote(
        instrument=InstrumentId(instrument),
        exch_ts=ts,
        recv_ts=ts,
        bids=(Level(price=ltp - 0.05, qty=100, orders=1),),
        asks=(Level(price=ltp + 0.05, qty=100, orders=1),),
        ltp=ltp,
        volume=10000,
    )


def test_killswitch_persistence_and_atomic_write(tmp_path: Path) -> None:
    state_file = tmp_path / "data" / "state" / "killswitch.json"
    journal = FakeJournal()
    clock = FakeClock()

    ks = KillSwitch(state_file=state_file, journal=journal, clock=clock)
    assert ks.is_active() is False

    ks.trip(reason="Market turbulence", by="test")
    assert ks.is_active() is True
    assert state_file.exists()

    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["active"] is True
    assert data["reason"] == "Market turbulence"

    # Journal event must be durable
    assert len(journal.events) == 1
    assert journal.events[0]["kind"] == "killswitch.change"
    assert journal.events[0]["durable"] is True

    # Clear
    ks.clear(reason="Turbulence ended", by="test")
    assert ks.is_active() is False
    assert len(journal.events) == 2


def test_killswitch_cross_process_effect(tmp_path: Path) -> None:
    state_file = tmp_path / "data" / "state" / "killswitch.json"
    ks = KillSwitch(state_file=state_file)
    assert ks.is_active() is False

    # Simulate external process writing to killswitch.json
    state_file.parent.mkdir(parents=True, exist_ok=True)
    external_data = {
        "active": True,
        "reason": "external_cli_trip",
        "by": "process_2",
        "ts": "2026-10-27T09:30:00Z",
    }
    state_file.write_text(json.dumps(external_data), encoding="utf-8")

    # is_active checks mtime on every call, so it takes effect at once
    assert ks.is_active() is True
    assert ks.status()["reason"] == "external_cli_trip"


def test_risk_engine_blocks_on_active_killswitch(tmp_path: Path) -> None:
    state_file = tmp_path / "killswitch.json"
    ks = KillSwitch(state_file=state_file)
    ks.trip("Emergency stop", by="admin")

    engine = RiskEngine(killswitch=ks)
    clock = FakeClock()
    view = RiskViewContext(
        portfolio=None,
        positions={},
        open_orders=[],
        daily_pnl=Decimal("0"),
        latest_quotes={},
        params={},
        clock=clock,
    )
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:INFY"),
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
    )
    intent = PlaceOrder(spec=spec, intent_id="int-1")

    dec = engine.evaluate(intent, view)
    assert dec.allow is False
    assert dec.rule == "killswitch"
    assert "Emergency stop" in dec.reason


def test_risk_engine_data_integrity_unknown_and_missing_and_stale_quote(tmp_path: Path) -> None:
    ks = KillSwitch(state_file=tmp_path / "killswitch.json")
    clock = FakeClock()
    journal = FakeJournal()
    engine = RiskEngine(killswitch=ks, journal=journal)

    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:INFY"),
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
    )
    intent = PlaceOrder(spec=spec, intent_id="int-di")

    # 1. Unknown instrument
    inst_reliance = Instrument(
        id=InstrumentId("NSE:EQ:RELIANCE"),
        exchange="NSE",
        segment="EQ",
        symbol="RELIANCE",
        kind="EQUITY",
        lot_size=1,
        tick_size=Decimal("0.05"),
        expiry=None,
        underlying=None,
        isin=None,
        broker_keys={},
    )
    view_unknown = RiskViewContext(
        portfolio=None,
        positions={},
        open_orders=[],
        daily_pnl=Decimal("0"),
        latest_quotes={},
        params={"risk.conservative.stale_data_seconds": 5.0},
        clock=clock,
        instruments={InstrumentId("NSE:EQ:RELIANCE"): inst_reliance},
    )
    dec1 = engine.evaluate(intent, view_unknown)
    assert dec1.allow is False
    assert "unknown_instrument" in dec1.rule

    # 2. Missing quote
    view_no_quote = RiskViewContext(
        portfolio=None,
        positions={},
        open_orders=[],
        daily_pnl=Decimal("0"),
        latest_quotes={},
        params={"risk.conservative.stale_data_seconds": 5.0},
        clock=clock,
        instruments={},
    )
    dec2 = engine.evaluate(intent, view_no_quote)
    assert dec2.allow is False
    assert "missing_quote" in dec2.rule

    # 3. Stale quote (quote age 10s > limit 5s)
    q_stale = make_quote(recv_ts=clock.now() - timedelta(seconds=10))
    view_stale = RiskViewContext(
        portfolio=None,
        positions={},
        open_orders=[],
        daily_pnl=Decimal("0"),
        latest_quotes={InstrumentId("NSE:EQ:INFY"): q_stale},
        params={"risk.conservative.stale_data_seconds": 5.0},
        clock=clock,
        instruments={},
    )
    dec3 = engine.evaluate(intent, view_stale)
    assert dec3.allow is False
    assert "stale_data" in dec3.rule


def test_risk_engine_no_short_circuit_evaluates_all_rules(tmp_path: Path) -> None:
    ks = KillSwitch(state_file=tmp_path / "killswitch.json")
    clock = FakeClock()
    journal = FakeJournal()

    r1 = FakeRule("rule_1", allow=False, values={"v1": 10})
    r2 = FakeRule("rule_2", allow=False, values={"v2": 20})
    r3 = FakeRule("rule_3", allow=True, values={"v3": 30})

    engine = RiskEngine(killswitch=ks, rules=[r1, r2, r3], journal=journal)

    q = make_quote(recv_ts=clock.now())
    view = RiskViewContext(
        portfolio=None,
        positions={},
        open_orders=[],
        daily_pnl=Decimal("0"),
        latest_quotes={InstrumentId("NSE:EQ:INFY"): q},
        params={"risk.conservative.stale_data_seconds": 5.0},
        clock=clock,
    )
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:INFY"),
        side=Side.BUY,
        qty=10,
        type=OrderType.MARKET,
    )
    intent = PlaceOrder(spec=spec, intent_id="int-multi")

    dec = engine.evaluate(intent, view)
    assert dec.allow is False
    # Both failing rules must be in rule name and reason (no short-circuit!)
    assert "rule_1" in dec.rule
    assert "rule_2" in dec.rule
    # All values merged
    assert dec.values["v1"] == 10
    assert dec.values["v2"] == 20
    assert dec.values["v3"] == 30

    # Ensure all rules were actually called
    assert len(r1.called_with) == 1
    assert len(r2.called_with) == 1
    assert len(r3.called_with) == 1


def test_risk_engine_reducing_intent_skips_entry_only_rules(tmp_path: Path) -> None:
    ks = KillSwitch(state_file=tmp_path / "killswitch.json")
    clock = FakeClock()

    r_daily_loss = FakeRule("daily_loss_limit_pct", allow=False, values={"loss": 2000})
    r_open_pos = FakeRule("max_open_positions", allow=False, values={"open": 6})
    r_position_pct = FakeRule("max_position_pct", allow=True, values={"pos": 10})

    engine = RiskEngine(killswitch=ks, rules=[r_daily_loss, r_open_pos, r_position_pct])

    # Existing long position: 20 shares of INFY
    pos_long = Position(
        instrument=InstrumentId("NSE:EQ:INFY"),
        product=Product.CNC,
        qty=20,
        avg_price=Decimal("100"),
        realized=Decimal("0"),
        unrealized=Decimal("0"),
    )
    q = make_quote(recv_ts=clock.now())
    view = RiskViewContext(
        portfolio=None,
        positions={"NSE:EQ:INFY:CNC": pos_long},
        open_orders=[],
        daily_pnl=Decimal("-2500"),
        latest_quotes={InstrumentId("NSE:EQ:INFY"): q},
        params={"risk.conservative.stale_data_seconds": 5.0},
        clock=clock,
    )

    # Selling 10 shares of INFY -> reducing!
    spec_sell = OrderSpec(
        instrument=InstrumentId("NSE:EQ:INFY"),
        side=Side.SELL,
        qty=10,
        type=OrderType.MARKET,
    )
    intent_sell = PlaceOrder(spec=spec_sell, intent_id="int-reduce")

    assert is_reducing_intent(intent_sell, view) is True

    dec = engine.evaluate(intent_sell, view)
    # Entry-only rules were skipped; max_position_pct passed -> Allow!
    assert dec.allow is True
    assert len(r_daily_loss.called_with) == 0
    assert len(r_open_pos.called_with) == 0
    assert len(r_position_pct.called_with) == 1


def test_risk_monitor_daily_loss_trip(tmp_path: Path) -> None:
    ks = KillSwitch(state_file=tmp_path / "killswitch.json")
    assert ks.is_active() is False

    class MockPortfolio:
        def __init__(self, day_pnl: Decimal) -> None:
            self.day_pnl = day_pnl
            self.starting_cash = Decimal("100000.00")

    params = {"risk.conservative.daily_loss_limit_pct": 1.5}  # 1.5% of 100k = 1500
    port = MockPortfolio(day_pnl=Decimal("-1000.00"))
    monitor = RiskMonitor(ks, port, params, profile="conservative")

    # Loss 1000 < 1500 -> Does not trip
    fill_evt = FillEvent(
        fill=Fill(
            fill_id="f1",
            client_ref="r1",
            qty=1,
            price=Decimal("100"),
            ts=datetime.now(UTC),
            charges=Charges(),
            instrument=InstrumentId("NSE:EQ:INFY"),
            side=Side.BUY,
            product=Product.CNC,
        ),
        order=None,
    )
    assert monitor.on_fill(fill_evt) is False
    assert ks.is_active() is False

    # Loss reaches 1500 -> Trips killswitch!
    port.day_pnl = Decimal("-1500.00")
    assert monitor.on_fill(fill_evt) is True
    assert ks.is_active() is True
    assert "Daily loss" in ks.status()["reason"]


def test_cli_killswitch_commands(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path
    state_file = root / "data" / "state" / "killswitch.json"

    # Trip via CLI
    res_on = runner.invoke(app, ["--root", str(root), "killswitch", "on", "--reason", "demo_trip"])
    assert res_on.exit_code == 0
    assert "ACTIVATED" in res_on.output
    assert state_file.exists()

    # Status via CLI
    res_stat = runner.invoke(app, ["--root", str(root), "killswitch", "status"])
    assert res_stat.exit_code == 0
    assert "ACTIVE" in res_stat.output
    assert "demo_trip" in res_stat.output

    # Status as JSON
    res_json = runner.invoke(app, ["--root", str(root), "--json", "killswitch", "status"])
    assert res_json.exit_code == 0
    data = json.loads(res_json.output)
    assert data["active"] is True

    # Clear via CLI
    res_off = runner.invoke(
        app, ["--root", str(root), "killswitch", "off", "--reason", "demo_cleared"]
    )
    assert res_off.exit_code == 0
    assert "CLEARED" in res_off.output

    # Status cleared
    res_stat2 = runner.invoke(app, ["--root", str(root), "killswitch", "status"])
    assert "CLEARED" in res_stat2.output


def test_risk_engine_observe_mode_blocks_non_reducing(tmp_path: Path) -> None:
    ks = KillSwitch(state_file=tmp_path / "killswitch.json")
    clock = FakeClock()
    engine = RiskEngine(killswitch=ks, rules=[], observe=True)

    pos_long = Position(
        instrument=InstrumentId("NSE:EQ:INFY"),
        product=Product.CNC,
        qty=10,
        avg_price=Decimal("100"),
        realized=Decimal("0"),
        unrealized=Decimal("0"),
    )
    q = make_quote(recv_ts=clock.now())
    view = RiskViewContext(
        portfolio=None,
        positions={"NSE:EQ:INFY:CNC": pos_long},
        open_orders=[],
        daily_pnl=Decimal("0"),
        latest_quotes={
            InstrumentId("NSE:EQ:INFY"): q,
            InstrumentId("NSE:EQ:TCS"): q,
        },
        params={"risk.conservative.stale_data_seconds": 5.0},
        clock=clock,
    )

    # 1. Non-reducing BUY on INFY (already long 10)
    buy_spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:INFY"), side=Side.BUY, qty=5, type=OrderType.MARKET
    )
    dec_buy = engine.evaluate(PlaceOrder(spec=buy_spec, intent_id="i-1"), view)
    assert dec_buy.allow is False
    assert dec_buy.rule == "observe_mode"
    assert "observe mode" in dec_buy.reason

    # 2. Reducing SELL on INFY (selling 5 of 10)
    sell_reducing = OrderSpec(
        instrument=InstrumentId("NSE:EQ:INFY"), side=Side.SELL, qty=5, type=OrderType.MARKET
    )
    dec_red = engine.evaluate(PlaceOrder(spec=sell_reducing, intent_id="i-2"), view)
    assert dec_red.allow is True

    # 3. Non-reducing SELL on TCS (0 position)
    tcs_spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:TCS"), side=Side.SELL, qty=5, type=OrderType.MARKET
    )
    dec_tcs = engine.evaluate(PlaceOrder(spec=tcs_spec, intent_id="i-3"), view)
    assert dec_tcs.allow is False
    assert "observe mode" in dec_tcs.reason
