"""SMA Cross Strategy plugin implementation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import time as dt_time
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

from .config_schema import SMACrossConfig


class SMACrossStrategy(PluginBase):
    """Long-only Simple Moving Average crossover strategy demo."""

    params_schema = SMACrossConfig

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, object] | None = None,
        ctx: PluginContext | Any = None,
    ) -> None:
        super().__init__(manifest, config or {}, ctx)
        self._cfg = SMACrossConfig.model_validate(self.config)
        self._instruments: set[InstrumentId] = {InstrumentId(s) for s in self._cfg.instruments}
        self._prev_fast: dict[InstrumentId, float] = {}
        self._prev_slow: dict[InstrumentId, float] = {}
        self._entry_prices: dict[InstrumentId, float] = {}

    def doctor_checks(self) -> Sequence[DoctorCheck]:
        return (
            DoctorCheck(
                id="sma_cross.params",
                severity=Severity.WARNING,
                run=self._check_params,
            ),
        )

    def _check_params(self) -> CheckResult:
        if self._cfg.fast >= self._cfg.slow:
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message=(
                    f"Fast period ({self._cfg.fast}) must be less "
                    f"than slow period ({self._cfg.slow})"
                ),
                fix="Set fast < slow in strategy parameters",
            )
        return CheckResult(ok=True, message="SMA cross parameters are valid")

    def on_start(self, ctx: StrategyContext) -> None:
        """Initialize monitored instruments from context if none configured."""
        if not self._instruments and ctx.instruments:
            self._instruments = set(ctx.instruments.keys())

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> Sequence[Intent]:
        """Process completed bars and emit entry/exit PlaceOrder intents."""
        if not getattr(bar, "complete", True):
            return ()
        if bar.timeframe != self._cfg.timeframe:
            return ()
        if self._instruments and bar.instrument not in self._instruments:
            return ()

        if ctx.indicator is None:
            return ()

        fast_val = ctx.indicator("sma", bar.instrument, self._cfg.timeframe, period=self._cfg.fast)
        slow_val = ctx.indicator("sma", bar.instrument, self._cfg.timeframe, period=self._cfg.slow)
        if fast_val is None or slow_val is None:
            return ()

        prev_fast = self._prev_fast.get(bar.instrument)
        prev_slow = self._prev_slow.get(bar.instrument)
        self._prev_fast[bar.instrument] = fast_val
        self._prev_slow[bar.instrument] = slow_val

        if prev_fast is None or prev_slow is None:
            return ()

        pos = ctx.position(bar.instrument) if ctx.position is not None else None
        current_qty = pos.qty if pos is not None else 0

        product_enum = Product.CNC if self._cfg.product.lower() == "cnc" else Product.MIS

        # 1. Exit check: Fast crosses below slow
        if prev_fast >= prev_slow and fast_val < slow_val and current_qty > 0:
            self._entry_prices.pop(bar.instrument, None)
            return (
                self._make_order_intent(
                    bar.instrument,
                    Side.SELL,
                    current_qty,
                    product_enum,
                    "sma_cross_exit",
                    f"exit_cross_{bar.instrument}_{bar.end_ts.timestamp()}",
                ),
            )

        # 2. Stop-loss check
        if self._cfg.stop_pct is not None and current_qty > 0:
            entry_p = self._entry_prices.get(bar.instrument)
            if entry_p is not None and bar.close <= entry_p * (1.0 - self._cfg.stop_pct):
                self._entry_prices.pop(bar.instrument, None)
                return (
                    self._make_order_intent(
                        bar.instrument,
                        Side.SELL,
                        current_qty,
                        product_enum,
                        "sma_cross_stop_loss",
                        f"exit_stop_{bar.instrument}_{bar.end_ts.timestamp()}",
                    ),
                )

        # 3. Entry check: Fast crosses above slow (long-only, no current position)
        if prev_fast <= prev_slow and fast_val > slow_val and current_qty == 0:
            if self._cfg.exit_time:
                try:
                    eh, em = map(int, self._cfg.exit_time.split(":"))
                    ist_zone = ZoneInfo("Asia/Kolkata")
                    bar_dt = bar.end_ts
                    if ctx.clock is not None:
                        now_c = ctx.clock.now()
                        if now_c is not None:
                            bar_dt = now_c
                    ist_dt = bar_dt.astimezone(ist_zone)
                    if ist_dt.time() >= dt_time(hour=eh, minute=em):
                        return ()
                except Exception:
                    pass
            self._entry_prices[bar.instrument] = bar.close
            return (
                self._make_order_intent(
                    bar.instrument,
                    Side.BUY,
                    self._cfg.qty,
                    product_enum,
                    "sma_cross_entry",
                    f"entry_{bar.instrument}_{bar.end_ts.timestamp()}",
                ),
            )

        return ()

    def _make_order_intent(
        self,
        instrument: InstrumentId,
        side: Side,
        qty: int,
        product: Product,
        tag: str,
        intent_id: str,
    ) -> PlaceOrder:
        spec = OrderSpec(
            instrument=instrument,
            side=side,
            qty=qty,
            type=OrderType.MARKET,
            price=None,
            product=product,
            validity=Validity.DAY,
            tag=tag,
        )
        return PlaceOrder(spec=spec, intent_id=intent_id)

    def on_tick(self, ctx: StrategyContext, tick: Tick) -> Sequence[Intent]:
        """Bar-driven strategy ignores raw ticks."""
        return ()

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> Sequence[Intent]:
        """Trade fills recorded without reactive order generation."""
        return ()

    def on_timer(self, ctx: StrategyContext, name: str) -> Sequence[Intent]:
        """Exit all open positions on intraday square-off timer."""
        exit_triggers = {"exit", "exit_time", "square_off", self._cfg.exit_time}
        if name not in exit_triggers:
            return ()

        intents: list[Intent] = []
        product_enum = Product.CNC if self._cfg.product.lower() == "cnc" else Product.MIS
        now_ts = ctx.clock.now().timestamp() if ctx.clock else 0.0

        for inst in self._instruments:
            pos = ctx.position(inst) if ctx.position is not None else None
            if pos is not None and pos.qty > 0:
                self._entry_prices.pop(inst, None)
                intents.append(
                    self._make_order_intent(
                        inst,
                        Side.SELL,
                        pos.qty,
                        product_enum,
                        "sma_cross_timer_exit",
                        f"timer_exit_{inst}_{now_ts}",
                    )
                )
        return intents

    def on_stop(self, ctx: StrategyContext) -> None:
        """Lifecycle stop hook resetting tracking structures."""
        self._instruments.clear()
        self._prev_fast.clear()
        self._prev_slow.clear()
        self._entry_prices.clear()

    def state(self) -> Mapping[str, object]:
        """Serialize internal state for checkpointing."""
        return {
            "prev_fast": {str(k): v for k, v in self._prev_fast.items()},
            "prev_slow": {str(k): v for k, v in self._prev_slow.items()},
            "entry_prices": {str(k): v for k, v in self._entry_prices.items()},
        }

    def restore(self, state: Mapping[str, object]) -> None:
        """Restore internal state from checkpoint."""
        pf = state.get("prev_fast", {})
        if isinstance(pf, Mapping):
            self._prev_fast = {InstrumentId(str(k)): float(v) for k, v in pf.items()}
        ps = state.get("prev_slow", {})
        if isinstance(ps, Mapping):
            self._prev_slow = {InstrumentId(str(k)): float(v) for k, v in ps.items()}
        ep = state.get("entry_prices", {})
        if isinstance(ep, Mapping):
            self._entry_prices = {InstrumentId(str(k)): float(v) for k, v in ep.items()}
