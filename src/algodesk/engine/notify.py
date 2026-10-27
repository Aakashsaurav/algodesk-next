"""Event-to-notification bridge translating engine events into concise messages."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from algodesk.api.market import FeedStatus
from algodesk.api.protocols import Clock, Message, Notifier, SystemClock
from algodesk.api.services import JournalWriter
from algodesk.api.trading import Fill
from algodesk.engine.events import Signal, SystemEvent

logger = logging.getLogger("algodesk.engine.notify")


class NotifyBridge:
    """Bridges engine events to registered Notifiers with failure isolation."""

    def __init__(
        self,
        notifiers: Sequence[Notifier],
        bus: Any | None = None,
        journal: JournalWriter | Any | None = None,
        clock: Clock | Any | None = None,
    ) -> None:
        self._notifiers = list(notifiers)
        self._bus = bus
        self._journal = journal
        self._clock: Clock = clock if clock is not None else SystemClock()

        self._first_signal_sent = False
        self._rejection_counts: dict[str, int] = {}

        if self._bus is not None and hasattr(self._bus, "subscribe"):
            self._subscribe_to_bus()

    def _subscribe_to_bus(self) -> None:
        try:
            if self._bus is not None and hasattr(self._bus, "subscribe"):
                self._bus.subscribe(SystemEvent, self._on_system_event)
        except Exception as exc:
            logger.debug("Could not subscribe NotifyBridge to SystemEvent: %s", exc)

    def _on_system_event(self, event: SystemEvent) -> None:
        if event.kind == "killswitch.change":
            active = bool(event.details.get("active", False))
            reason = str(event.details.get("reason", ""))
            self.notify_killswitch_change(active, reason)
        elif event.kind in ("feed.disconnected", "feed.gap"):
            self.notify_error(f"Feed issue: {event.message}", source="feed")
        elif event.kind == "strategy.error":
            self.notify_error(event.message, source="strategy")

    def send(self, message: Message) -> None:
        """Deliver message to all configured notifiers without raising into the engine."""
        for notifier in self._notifiers:
            try:
                notifier.send(message)
                if self._journal is not None and hasattr(self._journal, "append"):
                    try:
                        self._journal.append(
                            "alert.sent",
                            {
                                "title": message.title,
                                "text": message.text,
                                "severity": message.severity,
                            },
                        )
                    except Exception as j_exc:
                        logger.debug("Could not journal alert.sent: %s", j_exc)
            except Exception as exc:
                logger.warning("Notifier %s failed to send alert: %s", notifier, exc)
                if self._journal is not None and hasattr(self._journal, "append"):
                    try:
                        self._journal.append(
                            "alert.failed",
                            {
                                "title": message.title,
                                "text": message.text,
                                "error": str(exc),
                            },
                        )
                    except Exception:
                        pass

    def notify_session_start(self, run_name: str, mode: str, count: int) -> None:
        msg = Message(
            title="Session Started",
            text=f"AlgoDesk Next {mode} session '{run_name}' started with {count} instruments.",
            severity="info",
            channel="alerts",
            ts=self._clock.now(),
        )
        self.send(msg)

    def notify_feed_status(self, status: FeedStatus) -> None:
        state = status.state.lower()
        if state == "connected":
            msg = Message(
                title="Feed Connected",
                text=f"Market data feed connected: {status.details}",
                severity="info",
                channel="alerts",
                ts=self._clock.now(),
            )
        else:
            msg = Message(
                title="Feed Alert",
                text=f"Market data feed issue ({status.state}): {status.details}",
                severity="warning",
                channel="alerts",
                ts=self._clock.now(),
            )
        self.send(msg)

    def notify_signal(self, signal: Signal) -> None:
        if not self._first_signal_sent:
            self._first_signal_sent = True
            sig_txt = (
                f"First signal emitted: {signal.strategy_id} {signal.signal_type} for "
                f"{signal.instrument}"
            )
            msg = Message(
                title="First Signal",
                text=sig_txt,
                severity="info",
                channel="trades",
                ts=self._clock.now(),
            )
            self.send(msg)

    def notify_fill(self, fill: Fill) -> None:
        side_val = getattr(fill.side, "value", None)
        side_str = str(side_val if side_val is not None else fill.side)
        inst_str = str(fill.instrument) if fill.instrument else ""
        msg = Message(
            title="Trade Fill",
            text=f"Filled {side_str} {fill.qty} {inst_str} @ ₹{float(fill.price):.2f}",
            severity="info",
            channel="trades",
            ts=self._clock.now(),
        )
        self.send(msg)

    def record_risk_rejection(self, rule: str, reason: str) -> None:
        self._rejection_counts[rule] = self._rejection_counts.get(rule, 0) + 1

    def notify_risk_summary(self) -> None:
        if not self._rejection_counts:
            return
        summary_lines = [f"• {rule}: {count}" for rule, count in self._rejection_counts.items()]
        msg = Message(
            title="Risk Rejections Summary",
            text="Pre-trade risk rejections:\n" + "\n".join(summary_lines),
            severity="warning",
            channel="alerts",
            ts=self._clock.now(),
        )
        self.send(msg)

    def notify_killswitch_change(self, active: bool, reason: str) -> None:
        state_str = "ENGAGED" if active else "CLEARED"
        sev = "critical" if active else "info"
        msg = Message(
            title=f"Kill-Switch {state_str}",
            text=f"Kill-switch {state_str.lower()}: {reason}",
            severity=sev,
            channel="alerts",
            ts=self._clock.now(),
        )
        self.send(msg)

    def notify_error(self, what: str, source: str = "") -> None:
        src_label = f"[{source}] " if source else ""
        msg = Message(
            title="System Error",
            text=f"{src_label}{what}",
            severity="critical",
            channel="alerts",
            ts=self._clock.now(),
        )
        self.send(msg)

    def notify_heartbeat(self, status: str) -> None:
        msg = Message(
            title="Heartbeat",
            text=f"Heartbeat pulse: {status}",
            severity="info",
            channel="heartbeat",
            ts=self._clock.now(),
        )
        self.send(msg)

    def notify_daily_summary(self, summary_text: str) -> None:
        msg = Message(
            title="Daily Summary",
            text=summary_text,
            severity="info",
            channel="reports",
            ts=self._clock.now(),
        )
        self.send(msg)
