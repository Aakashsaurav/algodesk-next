"""Closed round-trip P&L and win rate computation using FIFO trade matching."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RoundTrip:
    """Record of a completed, closed trade round trip."""

    instrument: str
    qty: int
    open_side: str
    open_price: float
    close_price: float
    gross_pnl: float
    charges: float
    net_pnl: float
    is_net_win: bool
    is_gross_win: bool


def compute_round_trip_metrics(fills: Sequence[Any]) -> dict[str, Any]:
    """Calculate net and gross win rate across closed round trips using FIFO matching.

    Guarantees:
    - Matches fills FIFO per instrument to pair opening and closing fills.
    - Accurately charges transaction fees to both legs of the round trip.
    - Winning round trip requires net P&L > 0 (after all charges).
    - Also calculates gross win rate (gross P&L > 0 before charges).
    """
    if not fills:
        return {
            "round_trips": 0,
            "win_rate": 0.0,
            "gross_win_rate": 0.0,
            "winning_round_trips": 0,
            "gross_winning_round_trips": 0,
            "losing_round_trips": 0,
            "round_trip_details": [],
        }

    open_lots: dict[str, list[dict[str, Any]]] = defaultdict(list)
    closed: list[RoundTrip] = []

    for f in fills:
        inst = str(
            getattr(f, "instrument", "") or (f.get("instrument", "") if isinstance(f, dict) else "")
        )
        side_val = getattr(f, "side", None) or (f.get("side", "") if isinstance(f, dict) else "")
        side = side_val.value.upper() if hasattr(side_val, "value") else str(side_val).upper()
        qty = int(getattr(f, "qty", 0) or (f.get("qty", 0) if isinstance(f, dict) else 0))
        price = float(
            getattr(f, "price", 0.0) or (f.get("price", 0.0) if isinstance(f, dict) else 0.0)
        )

        chg_obj = getattr(f, "charges", None)
        if chg_obj is not None and hasattr(chg_obj, "total"):
            charges = float(chg_obj.total)
        elif isinstance(f, dict):
            charges = float(f.get("charges", 0.0))
        else:
            charges = float(getattr(f, "charges", 0.0) or 0.0)

        charge_per_unit = charges / qty if qty > 0 else 0.0
        remaining_qty = qty

        lots = open_lots[inst]
        while remaining_qty > 0 and lots and lots[0]["side"] != side:
            open_lot = lots[0]
            matched_qty = min(remaining_qty, open_lot["qty"])
            open_price = open_lot["price"]
            open_side = open_lot["side"]

            if open_side == "BUY":
                gross_pnl = matched_qty * (price - open_price)
            else:
                gross_pnl = matched_qty * (open_price - price)

            allocated_charges = matched_qty * (open_lot["charge_per_unit"] + charge_per_unit)
            net_pnl = gross_pnl - allocated_charges

            closed.append(
                RoundTrip(
                    instrument=inst,
                    qty=matched_qty,
                    open_side=open_side,
                    open_price=round(open_price, 4),
                    close_price=round(price, 4),
                    gross_pnl=round(gross_pnl, 2),
                    charges=round(allocated_charges, 2),
                    net_pnl=round(net_pnl, 2),
                    is_net_win=net_pnl > 0.0,
                    is_gross_win=gross_pnl > 0.0,
                )
            )

            open_lot["qty"] -= matched_qty
            remaining_qty -= matched_qty
            if open_lot["qty"] <= 0:
                lots.pop(0)

        if remaining_qty > 0:
            lots.append(
                {
                    "side": side,
                    "qty": remaining_qty,
                    "price": price,
                    "charge_per_unit": charge_per_unit,
                }
            )

    total_rt = len(closed)
    net_wins = sum(1 for rt in closed if rt.is_net_win)
    gross_wins = sum(1 for rt in closed if rt.is_gross_win)
    net_win_rate = (net_wins / total_rt * 100.0) if total_rt > 0 else 0.0
    gross_win_rate = (gross_wins / total_rt * 100.0) if total_rt > 0 else 0.0

    return {
        "round_trips": total_rt,
        "win_rate": round(net_win_rate, 2),
        "gross_win_rate": round(gross_win_rate, 2),
        "winning_round_trips": net_wins,
        "gross_winning_round_trips": gross_wins,
        "losing_round_trips": total_rt - net_wins,
        "round_trip_details": closed,
    }
