"""Synchronous event bus with FIFO re-entrancy queue and handler isolation.

Guarantees:
1. Handlers run synchronously in subscription order.
2. Events published inside a handler are queued FIFO and dispatched after the current handler.
3. Non-critical handler exceptions are passed to an error callback and swallowed.
4. Critical handler exceptions propagate immediately.
"""

from __future__ import annotations

import types
import typing
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _Subscription:
    handler: Callable[[Any], None]
    critical: bool


def _matches_type(event: Any, target_type: type | Any) -> bool:
    """Check if event matches target_type, supporting unions and protocols."""
    if target_type is Any or target_type is object:
        return True
    origin = typing.get_origin(target_type)
    if origin is typing.Union or origin is types.UnionType:
        args = typing.get_args(target_type)
        return any(_matches_type(event, a) for a in args)
    if isinstance(target_type, tuple):
        return any(_matches_type(event, t) for t in target_type)
    if isinstance(target_type, type):
        return isinstance(event, target_type)
    return type(event) is target_type


class EventBus:
    """Synchronous in-memory event dispatcher."""

    __slots__ = ("_dispatching", "_on_error", "_queue", "_subscriptions")

    def __init__(
        self,
        on_error: Callable[[Exception, Any, Callable[[Any], None]], None] | None = None,
    ) -> None:
        self._on_error = on_error
        self._subscriptions: list[tuple[type | Any, _Subscription]] = []
        self._queue: deque[Any] = deque()
        self._dispatching: bool = False

    def subscribe(
        self,
        event_type: type | Any,
        handler: Callable[[Any], None],
        *,
        critical: bool = False,
    ) -> None:
        """Subscribe handler to events matching event_type."""
        sub = _Subscription(handler=handler, critical=critical)
        self._subscriptions.append((event_type, sub))

    def unsubscribe(self, event_type: type | Any, handler: Callable[[Any], None]) -> bool:
        """Unsubscribe handler from event_type. Returns True if removed."""
        idx_to_remove = -1
        for i, (t, sub) in enumerate(self._subscriptions):
            if t == event_type and sub.handler == handler:
                idx_to_remove = i
                break
        if idx_to_remove >= 0:
            self._subscriptions.pop(idx_to_remove)
            return True
        return False

    def publish(self, event: Any) -> None:
        """Publish event synchronously.

        Nested publish calls are queued FIFO and processed in order.
        """
        self._queue.append(event)
        if self._dispatching:
            return

        self._dispatching = True
        try:
            self._drain_queue()
        finally:
            self._dispatching = False

    def _drain_queue(self) -> None:
        """Drain queued events in FIFO order."""
        while self._queue:
            event = self._queue.popleft()
            self._dispatch_single(event)

    def _dispatch_single(self, event: Any) -> None:
        """Dispatch a single event to all matching subscribers."""
        snapshot = list(self._subscriptions)
        for target_type, sub in snapshot:
            if not _matches_type(event, target_type):
                continue
            try:
                sub.handler(event)
            except Exception as exc:
                if self._on_error is not None:
                    self._on_error(exc, event, sub.handler)
                if sub.critical:
                    raise
