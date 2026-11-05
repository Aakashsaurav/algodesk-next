"""Order Manager owning the order state machine, client references, and order table.

Drives order placement, fills, timeouts, reconciliation, and event journaling.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from algodesk.api.errors import SafetyViolation
from algodesk.api.protocols import Clock, ExecutionVenue
from algodesk.api.services import JournalWriter, ParamReader
from algodesk.api.trading import (
    Fill,
    FillEvent,
    Order,
    OrderEvent,
    OrderEventMsg,
    OrderState,
    PlaceOrder,
    RiskDecision,
    VenueEvent,
    check_transition,
)


class OrderManager:
    """Manages order lifecycle, deterministic client references, and state transitions."""

    def __init__(
        self,
        bus: Any,
        journal: JournalWriter | Any,
        clock: Clock,
        venue: ExecutionVenue,
        fee_engine: Any,
        schedule: Any,
        params: ParamReader | Mapping[str, Any],
        run_id: str = "run",
    ) -> None:
        self.bus = bus
        self.journal = journal
        self.clock = clock
        self.venue = venue
        self.fee_engine = fee_engine
        self.schedule = schedule
        self.params = params
        self._run_id = run_id
        self._counter: int = 0
        self._orders: dict[str, Order] = {}
        self._seen_intents: set[str] = set()
        self._submitted_times: dict[str, datetime] = {}

        # Read submit timeout from params
        submit_ms = 0
        if hasattr(params, "get"):
            try:
                param_obj: Any = params.get("oms.submit_timeout_ms")
                if param_obj is not None:
                    if hasattr(param_obj, "value"):
                        submit_ms = int(param_obj.value)
                    else:
                        submit_ms = int(param_obj)
            except Exception:
                pass
        self.submit_timeout_ms = submit_ms

    @property
    def orders(self) -> Mapping[str, Order]:
        """Read-only view of managed orders."""
        return dict(self._orders)

    def get_order(self, client_ref: str) -> Order | None:
        """Retrieve order by client reference."""
        return self._orders.get(client_ref)

    def _transition(self, order: Order, to_state: OrderState, reason: str = "") -> None:
        """Drive order state machine, journal order.event, and publish OrderEventMsg."""
        from_state = order.state
        try:
            check_transition(from_state, to_state)
        except SafetyViolation as e:
            if hasattr(self.journal, "append"):
                self.journal.append(
                    "error",
                    {
                        "error": "SafetyViolation",
                        "what": str(e),
                        "client_ref": order.client_ref,
                        "from_state": from_state.value,
                        "to_state": to_state.value,
                    },
                    durable=True,
                )
            raise

        object.__setattr__(order, "state", to_state)
        now_ts = self.clock.now()

        if hasattr(self.journal, "append"):
            self.journal.append(
                "order.event",
                {
                    "client_ref": order.client_ref,
                    "from_state": from_state.value,
                    "to_state": to_state.value,
                    "ts": now_ts.isoformat(),
                    "reason": reason,
                },
            )

        if self.bus is not None and hasattr(self.bus, "publish"):
            evt = OrderEvent(
                client_ref=order.client_ref,
                from_state=from_state,
                to_state=to_state,
                ts=now_ts,
                reason=reason,
            )
            self.bus.publish(OrderEventMsg(event=evt, order=order))

    async def submit(self, intent: PlaceOrder, decision: RiskDecision) -> Order:
        """Submit PlaceOrder with approved RiskDecision.

        Journals intent and order.created with durable=True BEFORE calling venue.place.
        """
        # INV-3: Refuse any intent without an approved RiskDecision
        if decision is None or not decision.allow:
            raise SafetyViolation(
                what=f"Cannot submit order without approved RiskDecision: {decision}",
                where="OrderManager.submit",
                fix="Ensure RiskEngine approves the intent before OMS submission",
            )

        # Reject duplicate intent_id
        if not intent.intent_id or intent.intent_id in self._seen_intents:
            raise SafetyViolation(
                what=f"Duplicate or empty intent_id rejected: {intent.intent_id!r}",
                where="OrderManager.submit",
                fix="Ensure every intent carries a unique intent_id",
            )
        self._seen_intents.add(intent.intent_id)

        # Deterministic client_ref: <run_id>-<counter>
        self._counter += 1
        client_ref = f"{self._run_id}-{self._counter}"
        created_ts = self.clock.now()

        order = Order(
            client_ref=client_ref,
            spec=intent.spec,
            state=OrderState.CREATED,
            created_ts=created_ts,
            filled_qty=0,
            avg_price=Decimal("0.00"),
            venue_ref=None,
        )
        self._orders[client_ref] = order

        # Write-ahead journaling: intent and order.created BEFORE venue.place
        if hasattr(self.journal, "append"):
            self.journal.append(
                "intent",
                {
                    "intent_id": intent.intent_id,
                    "client_ref": client_ref,
                    "instrument": str(intent.spec.instrument),
                    "side": intent.spec.side.value,
                    "qty": intent.spec.qty,
                    "type": intent.spec.type.value,
                    "price": str(intent.spec.price) if intent.spec.price is not None else None,
                    "product": intent.spec.product.value,
                    "validity": intent.spec.validity.value,
                    "tag": intent.spec.tag,
                },
                durable=True,
            )

            self.journal.append(
                "order.created",
                {
                    "client_ref": client_ref,
                    "intent_id": intent.intent_id,
                    "state": OrderState.CREATED.value,
                    "created_ts": created_ts.isoformat(),
                    "spec": {
                        "instrument": str(intent.spec.instrument),
                        "side": intent.spec.side.value,
                        "qty": intent.spec.qty,
                        "type": intent.spec.type.value,
                        "price": str(intent.spec.price) if intent.spec.price is not None else None,
                        "product": intent.spec.product.value,
                        "validity": intent.spec.validity.value,
                        "tag": intent.spec.tag,
                    },
                },
                durable=True,
            )

        # Drive state machine: CREATED -> SUBMITTED
        self._transition(order, OrderState.SUBMITTED, reason="submitted_to_venue")
        self._submitted_times[client_ref] = self.clock.now()

        # Submit to venue
        place_res = self.venue.place(order.spec, order.client_ref)
        ack = await place_res if inspect.isawaitable(place_res) else place_res

        if ack is not None:
            if hasattr(ack, "venue_ref") and ack.venue_ref:
                object.__setattr__(order, "venue_ref", ack.venue_ref)
            if hasattr(ack, "accepted") and not ack.accepted:
                self._transition(
                    order, OrderState.REJECTED, reason=getattr(ack, "reason", "rejected")
                )
                self._submitted_times.pop(client_ref, None)

        return order

    async def cancel(self, client_ref: str) -> None:
        """Cancel order by client reference."""
        order = self._orders.get(client_ref)
        if order is None:
            raise SafetyViolation(
                what=f"Cannot cancel unknown order: {client_ref}",
                where="OrderManager.cancel",
                fix="Verify client_ref exists before requesting cancellation",
            )

        self._transition(order, OrderState.CANCEL_REQUESTED, reason="cancel_requested")

        cancel_res = self.venue.cancel(client_ref)
        if inspect.isawaitable(cancel_res):
            await cancel_res

    def apply_venue_event(self, event: VenueEvent) -> None:
        """Apply venue event (ack, reject, partial, fill, cancel)."""
        order = self._orders.get(event.client_ref)
        if order is None:
            return

        if event.venue_ref:
            object.__setattr__(order, "venue_ref", event.venue_ref)

        kind = event.kind.lower()

        if kind == "ack":
            if order.state == OrderState.SUBMITTED:
                self._transition(order, OrderState.ACKNOWLEDGED, reason=event.reason or "venue_ack")
                self._submitted_times.pop(order.client_ref, None)

        elif kind == "reject":
            if order.state != OrderState.REJECTED:
                self._transition(order, OrderState.REJECTED, reason=event.reason or "venue_reject")
            self._submitted_times.pop(order.client_ref, None)

        elif kind == "cancel":
            if order.state != OrderState.CANCELLED:
                self._transition(order, OrderState.CANCELLED, reason=event.reason or "venue_cancel")
            self._submitted_times.pop(order.client_ref, None)

        elif kind in ("partial", "fill", "partially_filled", "filled"):
            gf = event.gross_fill
            fill_qty = gf.qty if gf is not None else event.qty
            fill_price = gf.price if gf is not None else event.price
            fill_id = (
                gf.venue_fill_id
                if gf is not None
                else (event.venue_fill_id or f"fill-{self._counter}")
            )
            fill_ts = gf.ts if gf is not None else event.ts

            # Calculate charges with fee engine
            segment = "eq"
            if ":" in str(order.spec.instrument):
                parts = str(order.spec.instrument).split(":")
                if len(parts) >= 2:
                    segment = parts[1].lower()

            turnover = Decimal(fill_qty) * fill_price
            charges = self.fee_engine.calculate_trade_charges(
                turnover=turnover,
                side=order.spec.side,
                segment=segment,
                product=order.spec.product,
                trade_date=fill_ts.date(),
                schedule=self.schedule,
            )

            fill = Fill(
                fill_id=fill_id,
                client_ref=order.client_ref,
                qty=fill_qty,
                price=fill_price,
                ts=fill_ts,
                charges=charges,
                instrument=order.spec.instrument,
                side=order.spec.side,
                product=order.spec.product,
            )

            # Update order filled_qty and avg_price
            prior_qty = order.filled_qty
            prior_avg = order.avg_price
            new_qty = prior_qty + fill_qty
            new_avg = (
                (prior_avg * Decimal(prior_qty)) + (fill_price * Decimal(fill_qty))
            ) / Decimal(new_qty)
            object.__setattr__(order, "filled_qty", new_qty)
            object.__setattr__(order, "avg_price", new_avg)

            # State transition
            if new_qty >= order.spec.qty or kind in ("fill", "filled"):
                target_state = OrderState.FILLED
            else:
                target_state = OrderState.PARTIALLY_FILLED

            self._transition(order, target_state, reason=event.reason or f"{kind}_qty_{fill_qty}")
            self._submitted_times.pop(order.client_ref, None)

            # Journal fill
            if hasattr(self.journal, "append"):
                self.journal.append(
                    "fill",
                    {
                        "fill_id": fill.fill_id,
                        "client_ref": fill.client_ref,
                        "instrument": str(fill.instrument),
                        "side": fill.side.value if fill.side else "",
                        "qty": fill.qty,
                        "price": str(fill.price),
                        "ts": fill.ts.isoformat(),
                        "charges": {k: str(v) for k, v in fill.charges.items.items()},
                        "charges_total": str(fill.charges.total),
                    },
                )

            # Publish FillEvent
            if self.bus is not None and hasattr(self.bus, "publish"):
                self.bus.publish(FillEvent(fill=fill, order=order))

    async def check_timeouts(self) -> list[str]:
        """Check for unacknowledged submitted orders exceeding submit_timeout_ms.

        Moves order from SUBMITTED -> UNKNOWN -> RECONCILING, then queries venue.order_status.
        """
        now = self.clock.now()
        timed_out: list[str] = []

        for client_ref, sub_time in list(self._submitted_times.items()):
            order = self._orders.get(client_ref)
            if order is None or order.state != OrderState.SUBMITTED:
                self._submitted_times.pop(client_ref, None)
                continue

            elapsed_ms = (now - sub_time).total_seconds() * 1000.0
            if elapsed_ms >= self.submit_timeout_ms:
                timed_out.append(client_ref)
                self._submitted_times.pop(client_ref, None)

                # Drive through UNKNOWN -> RECONCILING
                self._transition(
                    order, OrderState.UNKNOWN, reason=f"submit_timeout_{int(elapsed_ms)}ms"
                )
                self._transition(
                    order, OrderState.RECONCILING, reason="reconciling_via_venue_status"
                )

                # Reconcile via venue.order_status
                status_res = self.venue.order_status(client_ref)
                status = await status_res if inspect.isawaitable(status_res) else status_res

                if status is not None:
                    target_state = status.state
                    self._transition(
                        order, target_state, reason=f"reconciled_to_{target_state.value}"
                    )
                    if status.venue_ref:
                        object.__setattr__(order, "venue_ref", status.venue_ref)
                    if status.filled_qty > 0:
                        object.__setattr__(order, "filled_qty", status.filled_qty)
                    if status.avg_price > Decimal("0.00"):
                        object.__setattr__(order, "avg_price", status.avg_price)

        return timed_out
