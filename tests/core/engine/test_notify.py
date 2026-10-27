"""Tests for NotifyBridge event translation and non-blocking delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from algodesk.api import (
    Charges,
    Fill,
    InstrumentId,
    Message,
    Product,
    Side,
)
from algodesk.api.market import FeedStatus
from algodesk.engine.bus import EventBus
from algodesk.engine.clock import SimClock
from algodesk.engine.events import Signal, SystemEvent
from algodesk.engine.notify import NotifyBridge


class MockNotifier:
    """Mock notifier recording messages sent or raising if requested."""

    def __init__(self, should_fail: bool = False) -> None:
        self.messages: list[Message] = []
        self.should_fail = should_fail

    def send(self, message: Message) -> bool:
        if self.should_fail:
            raise RuntimeError("Telegram connection failed")
        self.messages.append(message)
        return True


class MockJournal:
    """Mock journal recording append calls."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def append(self, event_type: str, data: dict[str, Any]) -> None:
        self.entries.append((event_type, data))


def test_notify_bridge_session_start_and_summary() -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    notifier = MockNotifier()
    journal = MockJournal()
    bridge = NotifyBridge(notifiers=[notifier], journal=journal, clock=clock)

    bridge.notify_session_start("demo_paper", "paper", 5)
    assert len(notifier.messages) == 1
    assert "started with 5 instruments" in notifier.messages[0].text
    assert notifier.messages[0].severity == "info"

    bridge.notify_daily_summary("Session completed: realized PnL = ₹1250.50")
    assert len(notifier.messages) == 2
    assert "Session completed" in notifier.messages[1].text
    assert len(journal.entries) == 2


def test_notify_bridge_feed_status() -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    notifier = MockNotifier()
    bridge = NotifyBridge(notifiers=[notifier], clock=clock)

    bridge.notify_feed_status(FeedStatus(state="connected", ts=clock.now(), details="all good"))
    assert len(notifier.messages) == 1
    assert notifier.messages[0].title == "Feed Connected"
    assert notifier.messages[0].severity == "info"

    bridge.notify_feed_status(FeedStatus(state="disconnected", ts=clock.now(), details="timeout"))
    assert len(notifier.messages) == 2
    assert notifier.messages[1].title == "Feed Alert"
    assert notifier.messages[1].severity == "warning"


def test_notify_bridge_first_signal_only() -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    notifier = MockNotifier()
    bridge = NotifyBridge(notifiers=[notifier], clock=clock)

    sig1 = Signal(
        strategy_id="sma_cross",
        instrument=InstrumentId("NSE:EQ:INFY"),
        signal_type="BUY",
        ts=clock.now(),
    )
    sig2 = Signal(
        strategy_id="sma_cross",
        instrument=InstrumentId("NSE:EQ:TCS"),
        signal_type="SELL",
        ts=clock.now(),
    )

    bridge.notify_signal(sig1)
    assert len(notifier.messages) == 1
    assert "First signal emitted" in notifier.messages[0].text

    bridge.notify_signal(sig2)
    assert len(notifier.messages) == 1


def test_notify_bridge_fill_and_rejections() -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    notifier = MockNotifier()
    bridge = NotifyBridge(notifiers=[notifier], clock=clock)

    fill = Fill(
        fill_id="f-101",
        client_ref="o-1",
        qty=25,
        price=Decimal("1550.00"),
        ts=clock.now(),
        charges=Charges(),
        instrument=InstrumentId("NSE:EQ:INFY"),
        side=Side.BUY,
        product=Product.CNC,
    )
    bridge.notify_fill(fill)
    assert len(notifier.messages) == 1
    assert "Filled BUY 25 NSE:EQ:INFY @ ₹1550.00" in notifier.messages[0].text
    assert notifier.messages[0].channel == "trades"

    bridge.record_risk_rejection("max_positions", "Limit of 3 exceeded")
    bridge.notify_risk_summary()
    assert len(notifier.messages) == 2
    assert "Risk Rejections Summary" in notifier.messages[1].title
    assert "max_positions: 1" in notifier.messages[1].text


def test_notify_bridge_failure_isolation() -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    notifier = MockNotifier(should_fail=True)
    journal = MockJournal()
    bridge = NotifyBridge(notifiers=[notifier], journal=journal, clock=clock)

    # Sending should not raise an exception even if notifier fails
    msg = Message(title="Test", text="Sample", severity="info", ts=clock.now())
    bridge.send(msg)

    assert len(notifier.messages) == 0
    assert len(journal.entries) == 1
    assert journal.entries[0][0] == "alert.failed"
    assert "Telegram connection failed" in journal.entries[0][1]["error"]


def test_notify_bridge_bus_subscription() -> None:
    bus = EventBus()
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    notifier = MockNotifier()
    _bridge = NotifyBridge(notifiers=[notifier], bus=bus, clock=clock)

    bus.publish(
        SystemEvent(
            kind="killswitch.change",
            message="Tripped on daily loss",
            details={"active": True, "reason": "Loss limit breached"},
            ts=clock.now(),
        )
    )
    assert len(notifier.messages) == 1
    assert "Kill-Switch ENGAGED" in notifier.messages[0].title
    assert "Loss limit breached" in notifier.messages[0].text
