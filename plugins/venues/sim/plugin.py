"""Simulated execution venue plugin entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence

from algodesk.api import (
    Funds,
    InstrumentId,
    OrderSpec,
    OrderStatus,
    PluginBase,
    PluginContext,
    PluginManifest,
    Position,
    Quote,
    VenueAck,
    VenueEvent,
)

from .venue import SimVenue


class SimVenuePlugin(PluginBase):
    """Execution venue plugin for paper and backtest simulations."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, object],
        ctx: PluginContext,
    ) -> None:
        super().__init__(manifest, config, ctx)
        params = dict(config or {})
        if ctx is not None and getattr(ctx, "params", None) is not None:
            sim_keys = (
                "sim.latency_ms",
                "sim.latency_jitter_ms",
                "sim.through_ticks",
                "sim.allow_short",
                "sim.reject_rate",
                "sim.timeout_rate",
                "sim.partial_fill_rate",
                "sim.seed",
            )
            for k in sim_keys:
                if k not in params:
                    try:
                        v = ctx.params.get(k)
                        if v is not None:
                            params[k] = v
                    except Exception:
                        pass
        self._venue = SimVenue(params=params)

    @property
    def venue(self) -> SimVenue:
        """Return the underlying SimVenue instance."""
        return self._venue

    def configure(self, instruments: Sequence[InstrumentId]) -> None:
        """Configure tradable instruments on venue."""
        self._venue.configure(instruments)

    async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:
        """Submit order to simulated venue."""
        return await self._venue.place(spec, client_ref)

    async def cancel(self, client_ref: str) -> VenueAck:
        """Cancel resting order on simulated venue."""
        return await self._venue.cancel(client_ref)

    def order_status(self, client_ref: str) -> OrderStatus | None:
        """Query order status by client reference."""
        return self._venue.order_status(client_ref)

    def end_of_day(self) -> None:
        """Process end of day order cancellations."""
        self._venue.end_of_day()

    def on_quote(self, quote: Quote) -> None:
        """Process incoming market quote."""
        self._venue.on_quote(quote)

    def events(self) -> AsyncIterator[VenueEvent]:
        """Stream venue execution and fill events."""
        return self._venue.events()

    def positions(self) -> Sequence[Position]:
        """Return simulated positions."""
        return self._venue.positions()

    def funds(self) -> Funds:
        """Return simulated virtual funds."""
        return self._venue.funds()
