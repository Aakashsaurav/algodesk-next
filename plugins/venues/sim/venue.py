"""Simulated execution venue matching orders against incoming quotes."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from algodesk.api import (
    Funds,
    GrossFill,
    Instrument,
    InstrumentId,
    OrderSpec,
    OrderState,
    OrderStatus,
    OrderType,
    Position,
    Product,
    Quote,
    Side,
    Validity,
    VenueAck,
    VenueEvent,
    parse_id,
    to_price,
)


@dataclass(slots=True)
class _SimOrder:
    client_ref: str
    venue_ref: str
    spec: OrderSpec
    trigger_recv_ts: datetime
    arrival: datetime
    state: OrderState
    is_marketable: bool = False
    marketable_checked: bool = False
    filled_qty: int = 0
    gross_fills: list[GrossFill] = field(default_factory=list)
    reason: str = ""


class SimVenue:
    """Simulated execution venue with depth matching and failure injection."""

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        p = dict(params or {})
        self.latency_ms = int(p.get("sim.latency_ms", 0))
        self.latency_jitter_ms = int(p.get("sim.latency_jitter_ms", 0))
        self.through_ticks = int(p.get("sim.through_ticks", 1))
        self.allow_short = bool(p.get("sim.allow_short", False))
        self.reject_rate = float(p.get("sim.reject_rate", 0.0))
        self.timeout_rate = float(p.get("sim.timeout_rate", 0.0))
        self.partial_fill_rate = float(p.get("sim.partial_fill_rate", 0.0))
        self.seed = int(p.get("sim.seed", 0))

        self._rng = random.Random(self.seed)
        self._instruments: dict[InstrumentId, Instrument] = {}
        self._configured_ids: set[InstrumentId] = set()
        self._latest_quotes: dict[InstrumentId, Quote] = {}
        self._orders: dict[str, _SimOrder] = {}
        self._resting_orders: list[str] = []
        self._positions: dict[tuple[InstrumentId, Product], int] = {}
        self._event_queue: asyncio.Queue[VenueEvent] = asyncio.Queue()
        self._event_history: list[VenueEvent] = []
        self._order_counter = 0
        self._fill_counter = 0
        self._cash = Decimal("10000000.00")
        self._last_ts: datetime = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)

    def configure(self, instruments: Sequence[InstrumentId | Instrument]) -> None:
        """Register instruments allowed for trading on this venue."""
        for item in instruments:
            if isinstance(item, Instrument):
                self._instruments[item.id] = item
                self._configured_ids.add(item.id)
            else:
                inst_id = InstrumentId(str(item))
                self._configured_ids.add(inst_id)
                if inst_id not in self._instruments:
                    self._register_default_instrument(inst_id)

    def _register_default_instrument(self, inst_id: InstrumentId) -> None:
        try:
            parts = parse_id(inst_id)
            self._instruments[inst_id] = Instrument(
                id=inst_id,
                exchange=parts.exchange,
                segment=parts.segment,
                symbol=parts.symbol,
                kind="EQ",
                lot_size=1,
                tick_size=Decimal("0.05"),
            )
        except Exception:
            self._instruments[inst_id] = Instrument(
                id=inst_id,
                exchange="NSE",
                segment="EQ",
                symbol=str(inst_id),
                kind="EQ",
                lot_size=1,
                tick_size=Decimal("0.05"),
            )

    def _get_trigger_ts(self, instrument: InstrumentId) -> datetime:
        quote = self._latest_quotes.get(instrument)
        if quote is not None:
            return quote.recv_ts
        return self._last_ts

    def _record_event(self, event: VenueEvent) -> None:
        self._event_history.append(event)
        self._event_queue.put_nowait(event)

    def _validate_order(self, spec: OrderSpec, client_ref: str) -> str | None:
        if spec.instrument not in self._configured_ids and spec.instrument not in self._instruments:
            return "unknown instrument"

        inst = self._instruments.get(spec.instrument)
        lot = inst.lot_size if inst else 1
        if spec.qty <= 0 or (spec.qty % lot != 0):
            return "quantity not a multiple of lot size"

        if spec.type == OrderType.LIMIT:
            if spec.price is None:
                return "limit price required"
            tick = inst.tick_size if inst else Decimal("0.05")
            rem = spec.price % tick
            if rem != Decimal("0") and rem != tick:
                return "price off the tick grid"

        if spec.side == Side.SELL and not self.allow_short:
            curr_pos = self._positions.get((spec.instrument, spec.product), 0)
            pending_sells = sum(
                self._orders[ref].spec.qty - self._orders[ref].filled_qty
                for ref in self._resting_orders
                if self._orders[ref].spec.instrument == spec.instrument
                and self._orders[ref].spec.product == spec.product
                and self._orders[ref].spec.side == Side.SELL
            )
            if curr_pos - pending_sells < spec.qty:
                return "sell beyond position"

        if self.reject_rate > 0.0 and self._rng.random() < self.reject_rate:
            return "injected random reject"

        return None

    def _check_marketable_at_submission(self, spec: OrderSpec) -> bool:
        if spec.type != OrderType.LIMIT or spec.price is None:
            return False
        quote = self._latest_quotes.get(spec.instrument)
        if quote is None:
            return False
        inst = self._instruments.get(spec.instrument)
        tick = inst.tick_size if inst else Decimal("0.05")
        if spec.side == Side.BUY and quote.asks:
            best_ask = to_price(quote.asks[0].price, tick)
            return spec.price >= best_ask
        if spec.side == Side.SELL and quote.bids:
            best_bid = to_price(quote.bids[0].price, tick)
            return spec.price <= best_bid
        return False

    async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:
        """Submit order to simulated venue, checking rejects, latency and arrival."""
        rejection_reason = self._validate_order(spec, client_ref)
        trigger_ts = self._get_trigger_ts(spec.instrument)

        if rejection_reason is not None:
            ack = VenueAck(
                client_ref=client_ref,
                venue_ref="",
                ts=trigger_ts,
                accepted=False,
                reason=rejection_reason,
            )
            self._record_event(
                VenueEvent(
                    kind="reject", client_ref=client_ref, ts=trigger_ts, reason=rejection_reason
                )
            )
            return ack

        if self.timeout_rate > 0.0 and self._rng.random() < self.timeout_rate:
            raise TimeoutError("Injected simulated venue timeout")

        jitter = 0
        if self.latency_jitter_ms > 0:
            jitter = self._rng.randint(-self.latency_jitter_ms, self.latency_jitter_ms)
        effective_delay = max(0, self.latency_ms + jitter)
        arrival = trigger_ts + timedelta(milliseconds=effective_delay)

        is_marketable = self._check_marketable_at_submission(spec)

        self._order_counter += 1
        venue_ref = f"SIM-{self._order_counter}"
        sim_order = _SimOrder(
            client_ref=client_ref,
            venue_ref=venue_ref,
            spec=spec,
            trigger_recv_ts=trigger_ts,
            arrival=arrival,
            state=OrderState.ACKNOWLEDGED,
            is_marketable=is_marketable,
        )
        self._orders[client_ref] = sim_order
        self._resting_orders.append(client_ref)

        ack = VenueAck(client_ref=client_ref, venue_ref=venue_ref, ts=trigger_ts, accepted=True)
        self._record_event(
            VenueEvent(kind="ack", client_ref=client_ref, venue_ref=venue_ref, ts=trigger_ts)
        )
        return ack

    async def cancel(self, client_ref: str) -> VenueAck:
        """Cancel active resting order."""
        order = self._orders.get(client_ref)
        ts = self._last_ts
        if order is None or client_ref not in self._resting_orders:
            return VenueAck(
                client_ref=client_ref,
                venue_ref="",
                ts=ts,
                accepted=False,
                reason="order not resting",
            )

        self._resting_orders.remove(client_ref)
        order.state = OrderState.CANCELLED
        order.reason = "cancelled_by_user"
        self._record_event(
            VenueEvent(
                kind="cancel",
                client_ref=client_ref,
                venue_ref=order.venue_ref,
                ts=ts,
                reason="cancelled_by_user",
            )
        )
        return VenueAck(client_ref=client_ref, venue_ref=order.venue_ref, ts=ts, accepted=True)

    def order_status(self, client_ref: str) -> OrderStatus | None:
        """Query current state and fills for an order."""
        order = self._orders.get(client_ref)
        if order is None:
            return None
        avg_price = Decimal("0.00")
        if order.gross_fills and order.filled_qty > 0:
            total_val = sum(
                (gf.price * Decimal(gf.qty) for gf in order.gross_fills), Decimal("0.00")
            )
            avg_price = total_val / Decimal(order.filled_qty)
        return OrderStatus(
            client_ref=client_ref,
            state=order.state,
            venue_ref=order.venue_ref,
            filled_qty=order.filled_qty,
            avg_price=avg_price,
            reason=order.reason,
        )

    def end_of_day(self) -> None:
        """Cancel all resting DAY orders at end of day."""
        for client_ref in list(self._resting_orders):
            order = self._orders.get(client_ref)
            if order is None:
                continue
            order.state = OrderState.CANCELLED
            order.reason = "end_of_day"
            self._record_event(
                VenueEvent(
                    kind="cancel",
                    client_ref=client_ref,
                    venue_ref=order.venue_ref,
                    ts=self._last_ts,
                    reason="end_of_day",
                )
            )
        self._resting_orders.clear()

    def on_quote(self, quote: Quote) -> None:
        """Process new market quote, updating book and matching eligible orders."""
        self._latest_quotes[quote.instrument] = quote
        self._last_ts = quote.recv_ts

        for client_ref in list(self._resting_orders):
            order = self._orders.get(client_ref)
            if order is None or order.spec.instrument != quote.instrument:
                continue
            if quote.recv_ts < order.arrival or quote.recv_ts <= order.trigger_recv_ts:
                continue

            self._match_order(order, quote)

    def _match_order(self, order: _SimOrder, quote: Quote) -> None:
        if order.spec.type == OrderType.MARKET:
            self._match_market_order(order, quote)
        elif order.spec.type == OrderType.LIMIT:
            self._match_limit_order(order, quote)

    def _match_market_order(self, order: _SimOrder, quote: Quote) -> None:
        is_buy = order.spec.side == Side.BUY
        levels = quote.asks if is_buy else quote.bids
        inst = self._instruments.get(order.spec.instrument)
        tick = inst.tick_size if inst else Decimal("0.05")
        remaining = order.spec.qty - order.filled_qty

        for level in levels:
            if remaining <= 0 or level.qty <= 0:
                break
            fill_qty = min(remaining, level.qty)
            if (
                self.partial_fill_rate > 0.0
                and self._rng.random() < self.partial_fill_rate
                and fill_qty > 1
            ):
                fill_qty = max(1, fill_qty // 2)

            price = to_price(level.price, tick)
            self._apply_fill(order, fill_qty, price, quote.recv_ts)
            remaining -= fill_qty

        if remaining > 0:
            if order.client_ref in self._resting_orders:
                self._resting_orders.remove(order.client_ref)
            order.state = OrderState.CANCELLED
            order.reason = "insufficient depth"
            self._record_event(
                VenueEvent(
                    kind="cancel",
                    client_ref=order.client_ref,
                    venue_ref=order.venue_ref,
                    ts=quote.recv_ts,
                    reason="insufficient depth",
                )
            )
        elif order.client_ref in self._resting_orders:
            self._resting_orders.remove(order.client_ref)

    def _match_limit_order(self, order: _SimOrder, quote: Quote) -> None:
        is_buy = order.spec.side == Side.BUY
        levels = quote.asks if is_buy else quote.bids
        inst = self._instruments.get(order.spec.instrument)
        tick = inst.tick_size if inst else Decimal("0.05")
        limit_price = order.spec.price or Decimal("0.00")
        remaining = order.spec.qty - order.filled_qty

        # 1. Marketable matching across levels within limit price
        if order.is_marketable and not order.marketable_checked:
            order.marketable_checked = True
            for level in levels:
                if remaining <= 0 or level.qty <= 0:
                    break
                level_price = to_price(level.price, tick)
                if is_buy and level_price > limit_price:
                    break
                if not is_buy and level_price < limit_price:
                    break

                fill_qty = min(remaining, level.qty)
                if (
                    self.partial_fill_rate > 0.0
                    and self._rng.random() < self.partial_fill_rate
                    and fill_qty > 1
                ):
                    fill_qty = max(1, fill_qty // 2)

                self._apply_fill(order, fill_qty, level_price, quote.recv_ts)
                remaining -= fill_qty

            if remaining == 0:
                if order.client_ref in self._resting_orders:
                    self._resting_orders.remove(order.client_ref)
                return
            order.is_marketable = False

        # 2. IOC cancels remaining unfilled quantity
        if order.spec.validity == Validity.IOC:
            if order.client_ref in self._resting_orders:
                self._resting_orders.remove(order.client_ref)
            order.state = OrderState.CANCELLED
            order.reason = "ioc_unfilled"
            self._record_event(
                VenueEvent(
                    kind="cancel",
                    client_ref=order.client_ref,
                    venue_ref=order.venue_ref,
                    ts=quote.recv_ts,
                    reason="ioc_unfilled",
                )
            )
            return

        # 3. Non-marketable DAY order rests; check trade-through
        self._check_trade_through_fill(order, quote, remaining, limit_price, is_buy, tick)

    def _check_trade_through_fill(
        self,
        order: _SimOrder,
        quote: Quote,
        remaining: int,
        limit_price: Decimal,
        is_buy: bool,
        tick: Decimal,
    ) -> None:
        levels = quote.asks if is_buy else quote.bids
        if not levels:
            return
        best_price = Decimal(str(levels[0].price))
        through_delta = Decimal(self.through_ticks) * tick

        traded_through = False
        if is_buy and best_price <= (limit_price - through_delta):
            traded_through = True
        elif not is_buy and best_price >= (limit_price + through_delta):
            traded_through = True

        if traded_through:
            fill_qty = min(remaining, levels[0].qty)
            self._apply_fill(order, fill_qty, limit_price, quote.recv_ts)
            if order.filled_qty == order.spec.qty:
                if order.client_ref in self._resting_orders:
                    self._resting_orders.remove(order.client_ref)

    def _apply_fill(self, order: _SimOrder, fill_qty: int, price: Decimal, ts: datetime) -> None:
        self._fill_counter += 1
        fill_id = f"FILL-{self._fill_counter}"
        gross_fill = GrossFill(
            client_ref=order.client_ref,
            qty=fill_qty,
            price=price,
            ts=ts,
            venue_fill_id=fill_id,
        )
        order.gross_fills.append(gross_fill)
        order.filled_qty += fill_qty

        key = (order.spec.instrument, order.spec.product)
        curr = self._positions.get(key, 0)
        delta = fill_qty if order.spec.side == Side.BUY else -fill_qty
        self._positions[key] = curr + delta

        is_complete = order.filled_qty == order.spec.qty
        order.state = OrderState.FILLED if is_complete else OrderState.PARTIALLY_FILLED
        ev_kind = "fill" if is_complete else "partial"

        self._record_event(
            VenueEvent(
                kind=ev_kind,
                client_ref=order.client_ref,
                venue_ref=order.venue_ref,
                ts=ts,
                gross_fill=gross_fill,
                qty=fill_qty,
                price=price,
                venue_fill_id=fill_id,
            )
        )

    def positions(self) -> Sequence[Position]:
        """Return non-zero simulated positions."""
        result: list[Position] = []
        for (inst_id, prod), qty in self._positions.items():
            if qty != 0:
                result.append(
                    Position(
                        instrument=inst_id,
                        product=prod,
                        qty=qty,
                        avg_price=Decimal("0.00"),
                    )
                )
        return result

    def funds(self) -> Funds:
        """Return simulated virtual funds balance."""
        return Funds(available=self._cash, used=Decimal("0.00"), total=self._cash)

    async def events(self) -> AsyncIterator[VenueEvent]:
        """Yield venue events sequentially as they occur."""
        while True:
            event = await self._event_queue.get()
            yield event

    def drain_events(self) -> list[VenueEvent]:
        """Synchronously return all historical venue events."""
        return list(self._event_history)
