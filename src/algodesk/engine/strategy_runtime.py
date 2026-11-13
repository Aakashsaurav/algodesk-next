"""Strategy runtime hosting strategy plugins, lifecycle dispatch, and error isolation.

Dispatches market and timer events to loaded strategies, journals signals, evaluates intents
through the pre-trade risk engine, routes approved intents to the OMS, isolates strategy
exceptions to keep the session alive, and handles state serialization.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from algodesk.api.errors import ConfigError, PluginError
from algodesk.api.kinds import PluginKind
from algodesk.api.market import Bar, Tick
from algodesk.api.protocols import Clock, Strategy, StrategyContext, SystemClock
from algodesk.api.services import JournalWriter, PluginContext
from algodesk.api.trading import CancelOrder, Fill, Intent, PlaceOrder
from algodesk.engine.events import SystemEvent

logger = logging.getLogger("algodesk.engine.strategy_runtime")


class StrategyRuntime:
    """Hosts and executes strategy plugins with schema validation and failure isolation."""

    def __init__(
        self,
        registry: Any | None = None,
        order_manager: Any | None = None,
        risk_engine: Any | None = None,
        risk_view_builder: Callable[..., Any] | None = None,
        journal: JournalWriter | Any | None = None,
        clock: Clock | Any | None = None,
        bus: Any | None = None,
        notifier: Any | None = None,
        on_intent: Callable[[Any, datetime], None] | None = None,
        on_reject: Callable[[Any, Any | None, str], None] | None = None,
    ) -> None:
        self._registry = registry
        self._order_manager = order_manager
        self._risk_engine = risk_engine
        self._risk_view_builder = risk_view_builder
        self._journal = journal
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._bus = bus
        self._notifier = notifier
        self._on_intent = on_intent
        self._on_reject = on_reject

        self._strategies: dict[str, Strategy] = {}
        self._contexts: dict[str, StrategyContext] = {}
        self._halted: dict[str, bool] = {}
        self._params: dict[str, Any] = {}

    @property
    def strategies(self) -> Mapping[str, Strategy]:
        """Mapping of registered strategy instances by name."""
        return self._strategies

    def is_halted(self, name: str) -> bool:
        """Check whether a strategy has encountered an exception and halted."""
        return self._halted.get(name, False)

    def load_strategy(
        self,
        name: str,
        params: Mapping[str, Any],
        context: StrategyContext,
        strategy_instance: Strategy | None = None,
        plugin_ctx: PluginContext | None = None,
    ) -> Strategy:
        """Validate parameters and register or instantiate a strategy."""
        strat = strategy_instance
        if strat is None:
            if self._registry is None:
                raise PluginError(
                    what=f"Cannot load strategy '{name}': no PluginRegistry available",
                    where="StrategyRuntime.load_strategy",
                    fix="Provide a PluginRegistry to StrategyRuntime",
                )
            if plugin_ctx is None:
                raise PluginError(
                    what=f"Cannot load strategy '{name}': no PluginContext available",
                    where="StrategyRuntime.load_strategy",
                    fix="Provide a PluginContext to StrategyRuntime.load_strategy",
                )
            strat = self._registry.create(PluginKind.STRATEGY, name, config=params, ctx=plugin_ctx)

        # Schema-validate params if strategy exposes params_schema
        schema_cls = getattr(strat, "params_schema", None)
        validated_params: dict[str, Any] = dict(params)
        if schema_cls is not None:
            try:
                if hasattr(schema_cls, "model_validate"):
                    model = schema_cls.model_validate(params)
                    if hasattr(model, "model_dump"):
                        validated_params = model.model_dump()
                    else:
                        validated_params = dict(params)
                elif callable(schema_cls):
                    model = schema_cls(**params)
                    validated_params = dict(params)
            except Exception as exc:
                raise ConfigError(
                    what=f"Parameter schema validation failed for strategy '{name}': {exc}",
                    where=f"StrategyRuntime.load_strategy({name})",
                    fix=f"Verify parameter keys and types match schema for '{name}'",
                ) from exc

        self._strategies[name] = strat
        self._contexts[name] = context
        self._halted[name] = False
        self._params[name] = validated_params
        return strat

    def _handle_exception(self, name: str, hook: str, exc: Exception) -> None:
        """Isolate strategy failure: halt strategy, journal error, alert, keep session running."""
        self._halted[name] = True
        now_ts = self._clock.now()
        err_msg = str(exc)
        tb_str = traceback.format_exc()

        logger.error(
            "Strategy '%s' raised exception during %s: %s (halting strategy)",
            name,
            hook,
            err_msg,
        )

        if self._journal is not None and hasattr(self._journal, "append"):
            self._journal.append(
                "error",
                {
                    "strategy": name,
                    "hook": hook,
                    "what": f"Strategy {name} exception during {hook}: {err_msg}",
                    "error_type": type(exc).__name__,
                    "traceback": tb_str,
                },
            )

        if self._bus is not None and hasattr(self._bus, "publish"):
            self._bus.publish(
                SystemEvent(
                    kind="strategy.error",
                    message=f"Strategy {name} halted in {hook}: {err_msg}",
                    ts=now_ts,
                    details={"strategy": name, "hook": hook, "error": err_msg},
                )
            )

        if self._notifier is not None and hasattr(self._notifier, "send"):
            try:
                from algodesk.api.protocols import Message

                self._notifier.send(
                    Message(
                        title=f"Strategy Halted: {name}",
                        text=(
                            f"Strategy {name} encountered error in {hook}: {err_msg}. "
                            "Strategy halted; session continues."
                        ),
                        severity="critical",
                        channel="alerts",
                        ts=now_ts,
                    )
                )
            except Exception:
                pass

    async def _process_intents(self, name: str, intents: Sequence[Intent]) -> None:
        """Journal each signal, run pre-trade risk check, and submit approved intents to OMS."""
        if not intents:
            return

        now_ts = self._clock.now()

        for intent in intents:
            if self._on_intent is not None:
                try:
                    target_inst = getattr(getattr(intent, "spec", None), "instrument", None)
                    self._on_intent(target_inst, now_ts)
                except Exception:
                    pass

            # 1. Journal signal
            intent_id = getattr(intent, "intent_id", "")
            spec = getattr(intent, "spec", None)
            inst = getattr(spec, "instrument", "")
            side = getattr(spec, "side", "")
            qty = getattr(spec, "qty", 0)

            side_str = str(side.value if hasattr(side, "value") else side)
            logger.info(
                "Signal: strategy=%s intent=%s inst=%s side=%s qty=%s",
                name,
                type(intent).__name__,
                inst,
                side_str,
                qty,
            )

            if self._journal is not None and hasattr(self._journal, "append"):
                self._journal.append(
                    "signal",
                    {
                        "strategy": name,
                        "intent_id": intent_id,
                        "intent_type": type(intent).__name__,
                        "instrument": str(inst) if inst else "",
                        "side": side_str,
                        "qty": int(qty),
                        "ts": now_ts.isoformat(),
                    },
                )

            # 2. Risk evaluation
            decision = None
            if self._risk_engine is not None and self._risk_view_builder is not None:
                try:
                    view = self._risk_view_builder(strategy=name)
                except TypeError:
                    view = self._risk_view_builder()
                decision = self._risk_engine.evaluate(intent, view)

            if decision is not None and not decision.allow:
                logger.warning(
                    "Risk rejected signal: strategy=%s intent=%s inst=%s reason=%s",
                    name,
                    intent_id,
                    inst,
                    decision.reason,
                )
                if self._on_reject is not None:
                    self._on_reject(intent, decision, "risk")
                continue

            # 3. Submit to OMS if approved
            if decision is None or decision.allow:
                if self._order_manager is not None:
                    try:
                        if isinstance(intent, PlaceOrder):
                            await self._order_manager.submit(intent, decision)
                        elif isinstance(intent, CancelOrder):
                            await self._order_manager.cancel(intent.client_ref)
                    except Exception as exc:
                        logger.error("OMS rejected intent %s: %s", intent_id, exc)
                        if self._journal is not None and hasattr(self._journal, "append"):
                            self._journal.append(
                                "error",
                                {
                                    "component": "oms",
                                    "intent_id": intent_id,
                                    "error": str(exc),
                                    "ts": now_ts.isoformat(),
                                },
                            )
                        if self._on_reject is not None:
                            self._on_reject(intent, None, "oms")
                        raise

    def dispatch_start(self) -> None:
        """Call on_start on all active strategies."""
        for name, strat in list(self._strategies.items()):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                strat.on_start(ctx)
            except Exception as exc:
                self._handle_exception(name, "on_start", exc)

    async def dispatch_bar(self, bar: Bar) -> None:
        """Dispatch a completed bar to active strategies and process resulting intents."""
        for name, strat in list(self._strategies.items()):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                intents = strat.on_bar(ctx, bar)
                if intents:
                    await self._process_intents(name, intents)
            except Exception as exc:
                self._handle_exception(name, "on_bar", exc)

    async def dispatch_tick(self, tick: Tick) -> None:
        """Dispatch a market tick to active strategies and process resulting intents."""
        for name, strat in list(self._strategies.items()):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                intents = strat.on_tick(ctx, tick)
                if intents:
                    await self._process_intents(name, intents)
            except Exception as exc:
                self._handle_exception(name, "on_tick", exc)

    async def dispatch_fill(self, fill: Fill) -> None:
        """Dispatch trade fill to active strategies and process resulting intents."""
        for name, strat in list(self._strategies.items()):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                intents = strat.on_fill(ctx, fill)
                if intents:
                    await self._process_intents(name, intents)
            except Exception as exc:
                self._handle_exception(name, "on_fill", exc)

    async def dispatch_timer(self, timer_name: str) -> None:
        """Dispatch timer event to active strategies and process resulting intents."""
        for name, strat in list(self._strategies.items()):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                intents = strat.on_timer(ctx, timer_name)
                if intents:
                    await self._process_intents(name, intents)
            except Exception as exc:
                self._handle_exception(name, "on_timer", exc)

    def dispatch_stop(self) -> None:
        """Call on_stop on all active strategies."""
        for name, strat in list(self._strategies.items()):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                strat.on_stop(ctx)
            except Exception as exc:
                self._handle_exception(name, "on_stop", exc)

    def state(self) -> dict[str, Any]:
        """Serialize state of all loaded strategies."""
        res: dict[str, Any] = {}
        for name, strat in self._strategies.items():
            if hasattr(strat, "state") and callable(strat.state):
                try:
                    res[name] = strat.state()
                except Exception as exc:
                    logger.warning("Failed to serialize state for strategy '%s': %s", name, exc)
        return res

    def restore(self, states: Mapping[str, Any]) -> None:
        """Restore state across loaded strategies."""
        for name, st in states.items():
            strat = self._strategies.get(name)
            if strat is not None and hasattr(strat, "restore") and callable(strat.restore):
                try:
                    strat.restore(st)
                except Exception as exc:
                    logger.warning("Failed to restore state for strategy '%s': %s", name, exc)
