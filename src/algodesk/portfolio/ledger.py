"""Portfolio ledger tracking cash, positions, realized/unrealized P&L, and snapshots.

Operates strictly in Decimal for money and charges, adheres to exact accounting invariants.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from algodesk.api.market import InstrumentId, Quote
from algodesk.api.protocols import Clock
from algodesk.api.services import JournalWriter
from algodesk.api.trading import Fill, Position, Product, Side
from algodesk.api.trading import Portfolio as ApiPortfolio


class PortfolioLedger:
    """Ledger tracking cash, positions per (instrument, product), and portfolio equity."""

    def __init__(
        self,
        starting_cash: Decimal | str | float,
        journal: JournalWriter | Any | None = None,
        scheduler: Any = None,
        clock: Clock | None = None,
        snapshot_seconds: int = 60,
    ) -> None:
        self.starting_cash = Decimal(str(starting_cash))
        self.cash = self.starting_cash
        self._start_of_day_equity = self.starting_cash
        self.journal = journal
        self.scheduler = scheduler
        self.clock = clock
        self.snapshot_seconds = snapshot_seconds

        # Key: (InstrumentId, Product)
        self._positions: dict[tuple[InstrumentId, Product], Position] = {}
        self._total_realized = Decimal("0.00")
        self._total_charges = Decimal("0.00")
        self._latest_quotes: dict[InstrumentId, Quote] = {}

        # Set up periodic snapshot timer if scheduler and journal provided
        if (
            self.scheduler is not None
            and self.journal is not None
            and self.clock is not None
            and self.snapshot_seconds > 0
        ):
            start_ts = self.clock.now()
            if hasattr(self.scheduler, "schedule_repeating"):
                self.scheduler.schedule_repeating(
                    name="portfolio.snapshot",
                    start=start_ts,
                    interval_seconds=float(self.snapshot_seconds),
                    callback=self._on_snapshot_timer,
                )

    @property
    def positions(self) -> Mapping[tuple[InstrumentId, Product], Position]:
        """Read-only view of current positions."""
        return dict(self._positions)

    @property
    def realized_pnl(self) -> Decimal:
        """Gross realized P&L across all closed trades."""
        return self._total_realized

    @property
    def realized(self) -> Decimal:
        """Alias for realized_pnl."""
        return self._total_realized

    @property
    def total_charges(self) -> Decimal:
        """Total brokerage, regulatory, and exchange fees paid."""
        return self._total_charges

    @property
    def charges(self) -> Decimal:
        """Alias for total_charges."""
        return self._total_charges

    @property
    def net_realized_pnl(self) -> Decimal:
        """Net realized P&L after deducting all charges."""
        return self._total_realized - self._total_charges

    @property
    def unrealized_pnl(self) -> Decimal:
        """Sum of unrealized P&L across all positions marked to market."""
        return sum((p.unrealized for p in self._positions.values()), Decimal("0.00"))

    @property
    def unrealized(self) -> Decimal:
        """Alias for unrealized_pnl."""
        return self.unrealized_pnl

    @property
    def equity(self) -> Decimal:
        """Total portfolio equity: starting cash + realized + unrealized - charges."""
        return self.starting_cash + self._total_realized + self.unrealized_pnl - self._total_charges

    @property
    def day_pnl(self) -> Decimal:
        """Day P&L measured against start-of-day equity baseline."""
        return self.equity - self._start_of_day_equity

    @property
    def margin_used(self) -> Decimal:
        """Margin used for currently open positions."""
        # Cash-only accounting in M1 (leverage = 1)
        used = Decimal("0.00")
        for pos in self._positions.values():
            if pos.qty != 0:
                used += abs(Decimal(pos.qty)) * pos.avg_price
        return used

    def set_start_of_day_baseline(self, baseline: Decimal | None = None) -> None:
        """Set or reset the start-of-day baseline equity."""
        if baseline is not None:
            self._start_of_day_equity = Decimal(str(baseline))
        else:
            self._start_of_day_equity = self.equity

    def apply_fill(self, fill: Fill) -> Position:
        """Apply a trade fill to cash, positions, average price, realized P&L, and charges."""
        if fill.instrument is None or fill.product is None or fill.side is None:
            raise ValueError(f"Fill missing mandatory trade fields: {fill}")

        key = (fill.instrument, fill.product)
        signed_trade_qty = fill.qty if fill.side == Side.BUY else -fill.qty
        price = fill.price
        fee = fill.charges.total

        # Cash updates: BUY pays trade value + fee; SELL receives trade value - fee
        self.cash -= (Decimal(signed_trade_qty) * price) + fee
        self._total_charges += fee

        cur = self._positions.get(key)
        cur_qty = cur.qty if cur is not None else 0
        cur_avg = cur.avg_price if cur is not None else Decimal("0.00")
        cur_realized = cur.realized if cur is not None else Decimal("0.00")

        new_qty = cur_qty + signed_trade_qty
        realized_delta = Decimal("0.00")

        if cur_qty == 0:
            # Opening new position from flat
            new_avg = price
        elif (cur_qty > 0 and signed_trade_qty > 0) or (cur_qty < 0 and signed_trade_qty < 0):
            # Adding to existing position in same direction
            tot_qty = abs(cur_qty) + abs(signed_trade_qty)
            new_avg = (
                (Decimal(abs(cur_qty)) * cur_avg) + (Decimal(abs(signed_trade_qty)) * price)
            ) / Decimal(tot_qty)
        elif cur_qty * new_qty >= 0:
            # Reducing position without flipping (or closing to exactly 0)
            closed_qty = abs(signed_trade_qty)
            if cur_qty > 0:
                # Long reduction: sold at price vs cur_avg
                realized_delta = Decimal(closed_qty) * (price - cur_avg)
            else:
                # Short reduction: bought at price vs cur_avg
                realized_delta = Decimal(closed_qty) * (cur_avg - price)
            new_avg = cur_avg if new_qty != 0 else Decimal("0.00")
        else:
            # Position flip: trade closed existing position and opened opposite
            closed_qty = abs(cur_qty)
            if cur_qty > 0:
                # Long to Short flip
                realized_delta = Decimal(closed_qty) * (price - cur_avg)
            else:
                # Short to Long flip
                realized_delta = Decimal(closed_qty) * (cur_avg - price)
            new_avg = price

        self._total_realized += realized_delta
        pos_realized = cur_realized + realized_delta

        # Calculate new unrealized if quote available
        new_unrealized = Decimal("0.00")
        if new_qty != 0 and fill.instrument in self._latest_quotes:
            q = self._latest_quotes[fill.instrument]
            if new_qty > 0:
                mark_price = Decimal(str(q.bids[0].price)) if q.bids else Decimal(str(q.ltp))
                new_unrealized = Decimal(new_qty) * (mark_price - new_avg)
            else:
                mark_price = Decimal(str(q.asks[0].price)) if q.asks else Decimal(str(q.ltp))
                new_unrealized = Decimal(abs(new_qty)) * (new_avg - mark_price)

        pos = Position(
            instrument=fill.instrument,
            product=fill.product,
            qty=new_qty,
            avg_price=new_avg,
            realized=pos_realized,
            unrealized=new_unrealized,
        )
        self._positions[key] = pos
        return pos

    def mark(self, quote: Quote) -> None:
        """Mark positions to market: longs at best bid, shorts at best ask."""
        self._latest_quotes[quote.instrument] = quote

        for (inst, prod), pos in list(self._positions.items()):
            if inst != quote.instrument:
                continue

            if pos.qty > 0:
                # Longs mark at best bid
                mark_price = (
                    Decimal(str(quote.bids[0].price)) if quote.bids else Decimal(str(quote.ltp))
                )
                unrealized = Decimal(pos.qty) * (mark_price - pos.avg_price)
            elif pos.qty < 0:
                # Shorts mark at best ask
                mark_price = (
                    Decimal(str(quote.asks[0].price)) if quote.asks else Decimal(str(quote.ltp))
                )
                unrealized = Decimal(abs(pos.qty)) * (pos.avg_price - mark_price)
            else:
                unrealized = Decimal("0.00")

            self._positions[(inst, prod)] = Position(
                instrument=pos.instrument,
                product=pos.product,
                qty=pos.qty,
                avg_price=pos.avg_price,
                realized=pos.realized,
                unrealized=unrealized,
            )

    def snapshot(self) -> ApiPortfolio:
        """Return the api Portfolio snapshot."""
        return ApiPortfolio(
            cash=self.cash,
            equity=self.equity,
            positions={
                f"{pos.instrument}:{pos.product.value}": pos for pos in self._positions.values()
            },
            margin_used=self.margin_used,
        )

    def _on_snapshot_timer(self) -> None:
        """Periodic snapshot callback appending portfolio.snapshot to journal."""
        if self.journal is not None and hasattr(self.journal, "append"):
            snap = self.snapshot()
            self.journal.append(
                "portfolio.snapshot",
                {
                    "cash": str(snap.cash),
                    "equity": str(snap.equity),
                    "margin_used": str(snap.margin_used),
                    "day_pnl": str(self.day_pnl),
                    "realized_pnl": str(self.realized_pnl),
                    "net_realized_pnl": str(self.net_realized_pnl),
                    "unrealized_pnl": str(self.unrealized_pnl),
                    "total_charges": str(self.total_charges),
                    "positions": {
                        k: {
                            "instrument": str(v.instrument),
                            "product": v.product.value,
                            "qty": v.qty,
                            "avg_price": str(v.avg_price),
                            "realized": str(v.realized),
                            "unrealized": str(v.unrealized),
                        }
                        for k, v in snap.positions.items()
                    },
                },
            )


Portfolio = PortfolioLedger
