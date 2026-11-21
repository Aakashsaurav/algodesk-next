"""Screen-driven rotation strategy plugin implementation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from algodesk.api import (
    CheckResult,
    DoctorCheck,
    InstrumentId,
    OrderSpec,
    OrderType,
    PlaceOrder,
    PluginBase,
    PluginContext,
    PluginManifest,
    Product,
    Severity,
    Side,
    Validity,
)
from algodesk.api.market import Bar, Tick
from algodesk.api.protocols import StrategyContext
from algodesk.api.trading import Fill, Intent

from .config_schema import ScreenRotationConfig

IST = ZoneInfo("Asia/Kolkata")


class ScreenRotationStrategy(PluginBase):
    """Long-only screen-driven portfolio rotation strategy."""

    params_schema = ScreenRotationConfig

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, object] | None = None,
        ctx: PluginContext | Any = None,
    ) -> None:
        super().__init__(manifest, config or {}, ctx)
        self._cfg = ScreenRotationConfig.model_validate(self.config)
        self._sessions_seen: int = 0
        self._rebalance_count: int = 0
        self._last_selection: tuple[str, ...] = ()
        self._current_target: tuple[str, ...] = ()
        self._last_session_date: date | None = None
        self._pending_orders: set[InstrumentId] = set()
        self.rebalances: list[dict[str, Any]] = []

    def doctor_checks(self) -> Sequence[DoctorCheck]:
        return (
            DoctorCheck(
                id="screen_rotation.params",
                severity=Severity.WARNING,
                run=self._check_params,
            ),
        )

    def _check_params(self) -> CheckResult:
        if self._cfg.top_n < 1:
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message="top_n must be >= 1",
                fix="Set top_n >= 1 in strategy parameters",
            )
        if self._cfg.rebalance_every < 1:
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message="rebalance_every must be >= 1",
                fix="Set rebalance_every >= 1 in strategy parameters",
            )
        return CheckResult(ok=True, message="Screen rotation parameters are valid")

    def on_start(self, ctx: StrategyContext) -> None:
        """Initialize state on strategy start."""
        self._pending_orders.clear()

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> Sequence[Intent]:
        """Process bar completion and trigger rebalances at scheduled intervals."""
        if not getattr(bar, "complete", True):
            return ()
        if bar.timeframe != self._cfg.timeframe:
            return ()

        # Map to IST session date
        b_dt: datetime = bar.end_ts
        bar_date = b_dt.astimezone(IST).date() if b_dt.tzinfo is not None else b_dt.date()

        # Only evaluate rebalance logic once per session date
        if self._last_session_date == bar_date:
            return ()

        self._last_session_date = bar_date
        self._sessions_seen += 1

        # Check if this session is a rebalance session
        # Counted from the first valid session: session 1, then every rebalance_every sessions
        is_rebalance = (
            self._sessions_seen == 1 or (self._sessions_seen - 1) % self._cfg.rebalance_every == 0
        )

        if not is_rebalance:
            return ()

        self._rebalance_count += 1

        # Call context screen
        selection = ctx.screen(self._cfg.screen, group=self._cfg.group)
        self._last_selection = tuple(selection.symbols)

        prev_target = set(self._current_target)

        # Deterministic ranking: (-score, symbol)
        sorted_syms = sorted(
            selection.symbols,
            key=lambda s: (-selection.scores.get(s, 0.0), str(s)),
        )
        target = tuple(sorted_syms[: self._cfg.top_n])

        intents: list[Intent] = []
        sold_symbols: list[str] = []
        bought_symbols: list[str] = []

        # 1. SELL positions no longer in target
        candidate_syms = set(prev_target)
        if ctx.instruments:
            candidate_syms.update(str(k) for k in ctx.instruments.keys())
        if hasattr(ctx.portfolio, "positions") and ctx.portfolio.positions:
            for p_key, p_val in ctx.portfolio.positions.items():
                if hasattr(p_val, "instrument"):
                    candidate_syms.add(str(p_val.instrument))
                elif isinstance(p_key, str):
                    candidate_syms.add(p_key.split(":")[0])

        for sym in sorted(candidate_syms):
            pos = ctx.position(sym) if ctx.position is not None else None
            if pos is not None and pos.qty > 0 and sym not in target:
                intents.append(
                    PlaceOrder(
                        spec=OrderSpec(
                            instrument=InstrumentId(sym),
                            side=Side.SELL,
                            qty=pos.qty,
                            type=OrderType.MARKET,
                            product=Product.CNC,
                            validity=Validity.DAY,
                        ),
                        intent_id=f"rot_{bar_date}_{sym}_SELL",
                    )
                )
                sold_symbols.append(sym)

        self._current_target = target

        # 2. BUY new targets: qty = floor(weight_pct% of portfolio equity / last close)
        equity = float(getattr(ctx.portfolio, "equity", 0.0))
        target_val = equity * (self._cfg.weight_pct / 100.0)

        for sym in target:
            pos = ctx.position(sym) if ctx.position is not None else None
            # Never buy if already held
            if pos is not None and pos.qty > 0:
                continue
            # Never exceed one open order per symbol
            inst_id = InstrumentId(sym)
            if inst_id in self._pending_orders:
                continue

            last_bars = ctx.bars(sym, self._cfg.timeframe, 1) if ctx.bars is not None else ()
            last_close = (
                float(last_bars[-1].close)
                if last_bars
                else (float(bar.close) if str(bar.instrument) == sym else 0.0)
            )
            if last_close <= 0.0:
                continue

            qty = int(math.floor(target_val / last_close))
            if qty <= 0:
                continue

            intents.append(
                PlaceOrder(
                    spec=OrderSpec(
                        instrument=inst_id,
                        side=Side.BUY,
                        qty=qty,
                        type=OrderType.MARKET,
                        product=Product.CNC,
                        validity=Validity.DAY,
                    ),
                    intent_id=f"rot_{bar_date}_{sym}_BUY",
                )
            )
            self._pending_orders.add(inst_id)
            bought_symbols.append(sym)

        # Record rebalance entry for rebalances.csv
        scores_parts = [f"{s}:{selection.scores.get(s, 0.0):.2f}" for s in target]
        self.rebalances.append(
            {
                "rebalance_date": str(bar_date),
                "screen_result_hash": str(selection.result_hash),
                "selected_symbols": ";".join(scores_parts),
                "bought": ";".join(bought_symbols),
                "sold": ";".join(sold_symbols),
            }
        )

        return tuple(intents)

    def on_tick(self, ctx: StrategyContext, tick: Tick) -> Sequence[Intent]:
        """Ticks ignored in daily rotation."""
        return ()

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        """Clear pending order flag when order is filled."""
        self._pending_orders.discard(fill.instrument)

    def on_timer(self, ctx: StrategyContext, name: str) -> Sequence[Intent]:
        """Timers ignored in daily rotation."""
        return ()

    def on_stop(self, ctx: StrategyContext) -> None:
        """Cleanup on stop."""
        self._pending_orders.clear()

    def state(self) -> Mapping[str, object]:
        """Serialize state for checkpointing."""
        return {
            "sessions_seen": self._sessions_seen,
            "rebalance_count": self._rebalance_count,
            "last_selection": list(self._last_selection),
            "current_target": list(self._current_target),
            "last_session_date": str(self._last_session_date) if self._last_session_date else None,
        }

    def restore(self, state: Mapping[str, object]) -> None:
        """Restore state from checkpoint."""
        self._sessions_seen = int(str(state.get("sessions_seen", 0)))
        self._rebalance_count = int(str(state.get("rebalance_count", 0)))
        raw_sel = state.get("last_selection", ())
        self._last_selection = (
            tuple(str(x) for x in raw_sel) if isinstance(raw_sel, (list, tuple)) else ()
        )
        raw_tgt = state.get("current_target", ())
        self._current_target = (
            tuple(str(x) for x in raw_tgt) if isinstance(raw_tgt, (list, tuple)) else ()
        )
        lsd = state.get("last_session_date")
        if lsd:
            self._last_session_date = date.fromisoformat(str(lsd))
        else:
            self._last_session_date = None
