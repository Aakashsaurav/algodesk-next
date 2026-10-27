"""Tests for synchronous EventBus, FIFO re-entrancy queue, and error isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from algodesk.api.market import FeedStatus, InstrumentId, MarketEvent, Tick
from algodesk.engine.bus import EventBus
from algodesk.engine.events import SystemEvent


def test_bus_basic_dispatch() -> None:
    bus = EventBus()
    received: list[SystemEvent] = []

    def handler(evt: SystemEvent) -> None:
        received.append(evt)

    bus.subscribe(SystemEvent, handler)
    now = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    evt = SystemEvent(kind="test", message="hello", ts=now)
    bus.publish(evt)

    assert len(received) == 1
    assert received[0].message == "hello"

    # Unsubscribe
    assert bus.unsubscribe(SystemEvent, handler) is True
    assert bus.unsubscribe(SystemEvent, handler) is False
    bus.publish(evt)
    assert len(received) == 1


def test_bus_union_matching() -> None:
    bus = EventBus()
    market_events: list[Any] = []

    bus.subscribe(MarketEvent, lambda e: market_events.append(e))

    now = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    tick = Tick(
        instrument=InstrumentId("NSE:EQ:INFY"),
        exch_ts=now,
        recv_ts=now,
        ltp=1500.0,
        ltq=10,
        volume=100.0,
    )
    status = FeedStatus(state="connected", ts=now)

    bus.publish(tick)
    bus.publish(status)
    assert len(market_events) == 2
    assert market_events[0] == tick
    assert market_events[1] == status


def test_bus_fifo_reentrancy() -> None:
    """Events published inside a handler must be queued FIFO and run after the outer handler."""
    bus = EventBus()
    trace: list[str] = []
    now = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)

    def handle_first(evt: SystemEvent) -> None:
        if evt.kind == "outer":
            trace.append("outer_start")
            bus.publish(SystemEvent(kind="nested_1", message="n1", ts=now))
            bus.publish(SystemEvent(kind="nested_2", message="n2", ts=now))
            trace.append("outer_end")
        elif evt.kind == "nested_1":
            trace.append("nested_1_start")
            bus.publish(SystemEvent(kind="nested_3", message="n3", ts=now))
            trace.append("nested_1_end")
        elif evt.kind == "nested_2":
            trace.append("nested_2_handled")
        elif evt.kind == "nested_3":
            trace.append("nested_3_handled")

    def handle_second(evt: SystemEvent) -> None:
        if evt.kind == "outer":
            trace.append("outer_handler_2")

    bus.subscribe(SystemEvent, handle_first)
    bus.subscribe(SystemEvent, handle_second)

    bus.publish(SystemEvent(kind="outer", message="start", ts=now))

    # Expect: outer_start, outer_end, outer_handler_2, nested_1..3 in FIFO order
    assert trace == [
        "outer_start",
        "outer_end",
        "outer_handler_2",
        "nested_1_start",
        "nested_1_end",
        "nested_2_handled",
        "nested_3_handled",
    ]


def test_bus_non_critical_error_isolation() -> None:
    errors: list[tuple[Exception, Any]] = []

    def on_err(exc: Exception, event: Any, handler: Any) -> None:
        errors.append((exc, event))

    bus = EventBus(on_error=on_err)
    success: list[str] = []

    def bad_handler(evt: SystemEvent) -> None:
        raise RuntimeError("flaky subscriber failed")

    def good_handler(evt: SystemEvent) -> None:
        success.append(evt.message)

    bus.subscribe(SystemEvent, bad_handler, critical=False)
    bus.subscribe(SystemEvent, good_handler, critical=False)

    now = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    bus.publish(SystemEvent(kind="log", message="ok", ts=now))

    # Exception swallowed and passed to callback; good_handler executed
    assert len(errors) == 1
    assert "flaky" in str(errors[0][0])
    assert success == ["ok"]


def test_bus_critical_error_propagation() -> None:
    bus = EventBus()

    def critical_handler(evt: SystemEvent) -> None:
        raise ValueError("critical risk failure")

    bus.subscribe(SystemEvent, critical_handler, critical=True)

    now = datetime(2026, 10, 26, 9, 15, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="critical risk failure"):
        bus.publish(SystemEvent(kind="risk", message="check", ts=now))
