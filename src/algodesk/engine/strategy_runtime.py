"""Strategy runtime hosting strategy plugins, lifecycle dispatch, and error isolation.

Dispatches market and timer events to loaded strategies, journals signals, evaluates intents
through the pre-trade risk engine, routes approved intents to the OMS, isolates strategy
exceptions to keep the session alive, and handles state serialization.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
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
        on_intent: Callable[..., None] | None = None,
        on_reject: Callable[[Any, Any | None, str], None] | None = None,
        params: Any | None = None,
        max_consecutive_errors: int | None = None,
    ) -> None:
        self._registry = registry
        self._order_manager = order_manager
        self._risk_engine = risk_engine
        self._risk_view_builder = risk_view_builder
        self._journal = journal
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._bus = bus
        self._notifier = notifier
        self._on_intent: Callable[..., None] | None = on_intent
        self._on_reject = on_reject

        consec_target = max_consecutive_errors
        if consec_target is None and params is not None:
            p_val = params.get("strategy.max_consecutive_errors")
            if p_val is not None:
                try:
                    consec_target = int(getattr(p_val, "value", p_val))
                except Exception:
                    pass
        if consec_target is None:
            default_count = 3
            consec_target = default_count
        self._max_consecutive_errors = max(1, consec_target)

        self._strategies: dict[str, Strategy] = {}
        self._contexts: dict[str, StrategyContext] = {}
        self._halted: dict[str, bool] = {}
        self._params: dict[str, Any] = {}
        self._consecutive_errors: dict[tuple[str, str], int] = {}
        self._strategy_errors: int = 0
        self._alerted_plugins: set[str] = set()

    @property
    def strategy_errors(self) -> int:
        """Total count of strategy hook exceptions encountered."""
        return self._strategy_errors

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

    def _handle_exception(
        self,
        name: str,
        hook: str,
        exc: Exception,
        instrument: str = "",
    ) -> None:
        """Isolate strategy failure: journal strategy.error, alert once, halt after limit."""
        from algodesk.api.protocols import Message
        from algodesk.api.scrub import Scrubber

        now_ts = self._clock.now()
        err_msg = str(exc)

        scrubber = Scrubber()
        clean_msg = (
            scrubber.scrub(err_msg).strip().splitlines()[0][:200] if err_msg else type(exc).__name__
        )

        # 1. Increment error counters
        key = (name, hook)
        self._consecutive_errors[key] = self._consecutive_errors.get(key, 0) + 1
        consec = self._consecutive_errors[key]
        self._strategy_errors += 1

        logger.error(
            "Strategy '%s' raised exception during %s (consecutive=%d): %s",
            name,
            hook,
            consec,
            clean_msg,
        )

        # 2. Journal strategy.error event
        if self._journal is not None and hasattr(self._journal, "append"):
            self._journal.append(
                "strategy.error",
                {
                    "plugin": name,
                    "hook": hook,
                    "instrument": instrument,
                    "exception_type": type(exc).__name__,
                    "message": clean_msg,
                    "consecutive_errors": consec,
                    "ts": now_ts.isoformat(),
                },
            )

        # 3. Publish system event
        if self._bus is not None and hasattr(self._bus, "publish"):
            self._bus.publish(
                SystemEvent(
                    kind="strategy.error",
                    message=f"Strategy {name} error in {hook}: {clean_msg}",
                    ts=now_ts,
                    details={
                        "plugin": name,
                        "hook": hook,
                        "instrument": instrument,
                        "error": clean_msg,
                        "consecutive_errors": consec,
                    },
                )
            )

        # 4. Telegram alert once per plugin per session
        if (
            name not in self._alerted_plugins
            and self._notifier is not None
            and hasattr(self._notifier, "send")
        ):
            try:
                self._notifier.send(
                    Message(
                        title=f"Strategy Error: {name}",
                        text=(
                            f"Strategy {name} encountered error in {hook}: {clean_msg}. "
                            "Hook isolated; session continues."
                        ),
                        severity="warning",
                        channel="alerts",
                        ts=now_ts,
                    )
                )
                self._alerted_plugins.add(name)
            except Exception:
                pass

        # 5. Halt strategy only if consecutive errors reached threshold
        if consec >= self._max_consecutive_errors:
            self._halted[name] = True
            logger.error(
                "Strategy '%s' reached max consecutive errors (%d) in %s; halting strategy",
                name,
                consec,
                hook,
            )
            if self._journal is not None and hasattr(self._journal, "append"):
                self._journal.append(
                    "strategy.halted",
                    {
                        "plugin": name,
                        "hook": hook,
                        "instrument": instrument,
                        "consecutive_errors": consec,
                        "reason": f"Hook {hook} failed {consec} times in a row: {clean_msg}",
                        "ts": now_ts.isoformat(),
                    },
                )
            if self._notifier is not None and hasattr(self._notifier, "send"):
                try:
                    self._notifier.send(
                        Message(
                            title=f"Strategy Halted: {name}",
                            text=(
                                f"Strategy {name} halted after {consec} consecutive errors in "
                                f"{hook}: {clean_msg}."
                            ),
                            severity="critical",
                            channel="alerts",
                            ts=now_ts,
                        )
                    )
                except Exception:
                    pass

    async def _process_intents(
        self,
        name: str,
        intents: Sequence[Intent],
        origin: str = "bar",
    ) -> None:
        """Journal each signal, run pre-trade risk check, and submit approved intents to OMS."""
        if not intents:
            return

        now_ts = self._clock.now()
        strat = self._strategies.get(name)
        ctx = self._contexts.get(name)
        sorted_intents = sorted(
            intents,
            key=lambda it: str(getattr(getattr(it, "spec", None), "instrument", "")),
        )

        for intent in sorted_intents:
            if self._on_intent is not None:
                try:
                    target_inst = getattr(getattr(intent, "spec", None), "instrument", None)
                    try:
                        self._on_intent(target_inst, now_ts, origin=origin, intent=intent)
                    except TypeError:
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
                if strat is not None and ctx is not None and hasattr(strat, "on_reject"):
                    on_rej_fn = getattr(strat, "on_reject", None)
                    if callable(on_rej_fn):
                        try:
                            on_rej_fn(ctx, intent, decision)
                            self._consecutive_errors[(name, "on_reject")] = 0
                        except Exception as exc:
                            self._handle_exception(name, "on_reject", exc, instrument=str(inst))
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
        for name, strat in sorted(self._strategies.items(), key=lambda x: str(x[0])):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                strat.on_start(ctx)
                self._consecutive_errors[(name, "on_start")] = 0
            except Exception as exc:
                self._handle_exception(name, "on_start", exc)

    async def dispatch_bar(self, bar: Bar) -> None:
        """Dispatch a completed bar to active strategies and process resulting intents."""
        inst_str = str(bar.instrument)
        for name, strat in sorted(self._strategies.items(), key=lambda x: str(x[0])):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                intents = strat.on_bar(ctx, bar)
                self._consecutive_errors[(name, "on_bar")] = 0
                if intents:
                    await self._process_intents(name, intents, origin="bar")
            except Exception as exc:
                self._handle_exception(name, "on_bar", exc, instrument=inst_str)

    async def dispatch_tick(self, tick: Tick) -> None:
        """Dispatch a market tick to active strategies and process resulting intents."""
        inst_str = str(tick.instrument)
        for name, strat in sorted(self._strategies.items(), key=lambda x: str(x[0])):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                intents = strat.on_tick(ctx, tick)
                self._consecutive_errors[(name, "on_tick")] = 0
                if intents:
                    await self._process_intents(name, intents, origin="tick")
            except Exception as exc:
                self._handle_exception(name, "on_tick", exc, instrument=inst_str)

    async def dispatch_fill(self, fill: Fill) -> None:
        """Dispatch trade fill to active strategies and process resulting intents."""
        inst_str = str(fill.instrument)
        for name, strat in sorted(self._strategies.items(), key=lambda x: str(x[0])):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                intents = strat.on_fill(ctx, fill)
                self._consecutive_errors[(name, "on_fill")] = 0
                if intents:
                    await self._process_intents(name, intents, origin="fill")
            except Exception as exc:
                self._handle_exception(name, "on_fill", exc, instrument=inst_str)

    async def dispatch_timer(self, timer_name: str) -> None:
        """Dispatch timer event to active strategies and process resulting intents."""
        for name, strat in sorted(self._strategies.items(), key=lambda x: str(x[0])):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                intents = strat.on_timer(ctx, timer_name)
                self._consecutive_errors[(name, "on_timer")] = 0
                if intents:
                    await self._process_intents(name, intents, origin="timer")
            except Exception as exc:
                self._handle_exception(name, "on_timer", exc)

    def dispatch_stop(self) -> None:
        """Call on_stop on all active strategies."""
        for name, strat in sorted(self._strategies.items(), key=lambda x: str(x[0])):
            if self._halted.get(name):
                continue
            ctx = self._contexts[name]
            try:
                strat.on_stop(ctx)
                self._consecutive_errors[(name, "on_stop")] = 0
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
