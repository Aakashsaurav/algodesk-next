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
    RiskDecision,
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
    """Long-only screen-driven portfolio rotation strategy with two-step rebalance."""

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
        self._pending_sells: set[str] = set()
        self._pending_buy_target: tuple[str, ...] = ()
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
        self._pending_sells.clear()
        self._pending_buy_target = ()

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> Sequence[Intent]:
        """Process bar completion and trigger rebalances at scheduled intervals."""
        if not getattr(bar, "complete", True):
            return ()
        if bar.timeframe != self._cfg.timeframe:
            return ()

        # Map to IST session date
        b_dt: datetime = bar.end_ts
        bar_date = b_dt.astimezone(IST).date() if b_dt.tzinfo is not None else b_dt.date()

        # Step 2 of two-step rebalance: Replacement buys on first completed bar after sells fill
        if self._pending_buy_target and not self._pending_sells:
            buys_to_place = list(self._pending_buy_target)
            self._pending_buy_target = ()
            buy_intents = self._generate_buys(ctx, bar, buys_to_place, bar_date)
            return tuple(buy_intents)

        # Only evaluate new rebalance logic once per session date
        if self._last_session_date == bar_date:
            return ()

        self._last_session_date = bar_date
        self._sessions_seen += 1

        # Check if this session is a rebalance session
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

        # Find held positions
        candidate_syms = set(prev_target)
        if ctx.instruments:
            candidate_syms.update(str(k) for k in ctx.instruments.keys())
        if hasattr(ctx.portfolio, "positions") and ctx.portfolio.positions:
            for p_key, p_val in ctx.portfolio.positions.items():
                if hasattr(p_val, "instrument"):
                    candidate_syms.add(str(p_val.instrument))
                elif isinstance(p_key, str):
                    candidate_syms.add(p_key.split(":")[0])

        held_syms: set[str] = set()
        for sym in sorted(candidate_syms):
            pos = ctx.position(sym) if ctx.position is not None else None
            if pos is not None and pos.qty > 0:
                held_syms.add(sym)

        # 1. Departing positions to sell
        sold_symbols: list[str] = []
        sell_intents: list[Intent] = []
        for sym in sorted(held_syms):
            if sym not in target:
                pos = ctx.position(sym) if ctx.position is not None else None
                if pos is not None and pos.qty > 0:
                    sell_intents.append(
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

        # 2. New target symbols to buy
        new_buy_symbols = [s for s in target if s not in held_syms]

        # Record rebalance entry for rebalances.csv
        scores_parts = [f"{s}:{selection.scores.get(s, 0.0):.2f}" for s in target]
        reb_dict: dict[str, Any] = {
            "rebalance_date": str(bar_date),
            "screen_result_hash": str(selection.result_hash),
            "selected_symbols": ";".join(scores_parts),
            "intended_sold": list(sold_symbols),
            "intended_bought": list(new_buy_symbols),
            "executed_sold": [],
            "executed_bought": [],
            "rejected": [],
        }
        self.rebalances.append(reb_dict)
        self._current_target = target

        # Two-step execution: if there are sells, emit sells now and defer buys
        if sold_symbols:
            self._pending_sells = set(sold_symbols)
            self._pending_buy_target = tuple(new_buy_symbols)
            return tuple(sell_intents)

        # If no sells needed (e.g. Session 1 or none departed), execute buys immediately
        if new_buy_symbols:
            buy_intents = self._generate_buys(ctx, bar, new_buy_symbols, bar_date)
            return tuple(buy_intents)

        return ()

    def _generate_buys(
        self,
        ctx: StrategyContext,
        bar: Bar,
        symbols_to_buy: Sequence[str],
        bar_date: date,
    ) -> list[Intent]:
        """Size and generate BUY intents from actual available equity and free cash."""
        equity = float(getattr(ctx.portfolio, "equity", 0.0))
        target_val = equity * (self._cfg.weight_pct / 100.0)

        # Sizing: determine portfolio capital and margin reserve from params or context
        portfolio_cash = float(getattr(ctx.portfolio, "cash", 0.0))
        eq_val = float(getattr(ctx.portfolio, "equity", 0.0))
        start_val = float(getattr(ctx.portfolio, "starting_cash", 0.0))

        # Read from parameter registry
        alloc_cap_param = None
        margin_param = None
        if ctx.params:
            alloc_cap_param = (
                ctx.params.get("allocated_capital")
                or ctx.params.get("starting_cash")
                or ctx.params.get("capital")
            )
            margin_param = (
                ctx.params.get("risk.conservative.margin_buffer_pct")
                or ctx.params.get("margin_buffer_pct")
                or ctx.params.get("buffer_pct")
            )

        if alloc_cap_param is not None:
            portfolio_base = float(getattr(alloc_cap_param, "value", alloc_cap_param))
        else:
            portfolio_base = start_val or eq_val or portfolio_cash

        if margin_param is not None:
            reserve_ratio = float(getattr(margin_param, "value", margin_param)) / 100.0
        else:
            reserve_ratio = 0.0

        required_reserve = reserve_ratio * portfolio_base
        available_funds = max(0.0, (portfolio_cash - required_reserve) * 0.98)

        n_buys = len(symbols_to_buy)
        per_stock_available = (available_funds / n_buys) if n_buys > 0 else target_val
        alloc_val = min(target_val, per_stock_available)

        intents: list[Intent] = []
        for sym in symbols_to_buy:
            pos = ctx.position(sym) if ctx.position is not None else None
            if pos is not None and pos.qty > 0:
                continue

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

            qty = int(math.floor(alloc_val / last_close))
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

        return intents

    def on_tick(self, ctx: StrategyContext, tick: Tick) -> Sequence[Intent]:
        """Ticks ignored in daily rotation."""
        return ()

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        """Update executed fills and clear pending order flags."""
        inst_str = str(fill.instrument)
        self._pending_orders.discard(fill.instrument)
        self._pending_sells.discard(inst_str)

        if self.rebalances:
            cur_reb = self.rebalances[-1]
            if fill.side == Side.SELL:
                if inst_str not in cur_reb["executed_sold"]:
                    cur_reb["executed_sold"].append(inst_str)
            elif fill.side == Side.BUY:
                if inst_str not in cur_reb["executed_bought"]:
                    cur_reb["executed_bought"].append(inst_str)

    def on_reject(
        self,
        ctx: StrategyContext,
        intent: Intent,
        decision: RiskDecision | None,
    ) -> None:
        """Track rejected intents for rebalances.csv and clean pending tracking."""
        if not isinstance(intent, PlaceOrder):
            return
        inst_str = str(intent.spec.instrument)
        self._pending_orders.discard(intent.spec.instrument)
        if intent.spec.side == Side.SELL:
            self._pending_sells.discard(inst_str)

        if self.rebalances and decision is not None and not decision.allow:
            cur_reb = self.rebalances[-1]
            rule_str = str(getattr(decision, "rule", "rejected"))
            rej_entry = f"{inst_str}:{rule_str}"
            if rej_entry not in cur_reb["rejected"]:
                cur_reb["rejected"].append(rej_entry)

    def on_timer(self, ctx: StrategyContext, name: str) -> Sequence[Intent]:
        """Timers ignored in daily rotation."""
        return ()

    def on_stop(self, ctx: StrategyContext) -> None:
        """Cleanup on stop."""
        self._pending_orders.clear()
        self._pending_sells.clear()
        self._pending_buy_target = ()

    def state(self) -> Mapping[str, object]:
        """Serialize state for checkpointing."""
        return {
            "sessions_seen": self._sessions_seen,
            "rebalance_count": self._rebalance_count,
            "last_selection": list(self._last_selection),
            "current_target": list(self._current_target),
            "last_session_date": str(self._last_session_date) if self._last_session_date else None,
            "pending_sells": list(self._pending_sells),
            "pending_buy_target": list(self._pending_buy_target),
            "rebalances": self.rebalances,
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
        raw_sells = state.get("pending_sells", [])
        if isinstance(raw_sells, list):
            self._pending_sells = set(str(x) for x in raw_sells)
        else:
            self._pending_sells = set()
        raw_buys = state.get("pending_buy_target", ())
        self._pending_buy_target = (
            tuple(str(x) for x in raw_buys) if isinstance(raw_buys, (list, tuple)) else ()
        )
        raw_reb = state.get("rebalances", [])
        self.rebalances = list(raw_reb) if isinstance(raw_reb, list) else []
