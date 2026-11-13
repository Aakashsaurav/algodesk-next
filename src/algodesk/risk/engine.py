"""Risk Engine evaluating pre-trade intents against data integrity, killswitch, and plugin rules.

Operates with strict fail-closed semantics and no short-circuiting across evaluated rules.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from algodesk.api.protocols import RiskRule
from algodesk.api.services import JournalWriter
from algodesk.api.trading import CancelOrder, Intent, PlaceOrder, Product, RiskDecision, Side
from algodesk.risk.killswitch import KillSwitch
from algodesk.risk.view import RiskViewContext

ENTRY_ONLY_RULES = {
    "daily_loss_limit_pct",
    "daily_loss",
    "max_open_positions",
    "entry_window",
    "duplicate_entry_guard",
}


def is_reducing_intent(intent: Intent, view: RiskViewContext) -> bool:
    """Determine if intent reduces or closes an existing open position."""
    if isinstance(intent, CancelOrder):
        return True

    if not isinstance(intent, PlaceOrder):
        return False

    spec = intent.spec
    inst_str = str(spec.instrument)
    prod_val = spec.product.value

    # Check matching position
    for pos in view.positions.values():
        p_inst = str(pos.instrument)
        p_prod = pos.product.value if isinstance(pos.product, Product) else str(pos.product)
        if p_inst == inst_str and p_prod == prod_val:
            if pos.qty > 0 and spec.side == Side.SELL:
                return True
            if pos.qty < 0 and spec.side == Side.BUY:
                return True

    return False


class RiskEngine:
    """Pre-trade risk evaluator running data integrity, kill-switch, and all rule plugins."""

    def __init__(
        self,
        killswitch: KillSwitch,
        rules: Sequence[RiskRule] = (),
        journal: JournalWriter | Any | None = None,
        observe: bool = False,
    ) -> None:
        self.killswitch = killswitch
        self.rules = list(rules)
        self.journal = journal
        self.observe = observe
        self.rejects_by_rule: dict[str, int] = {}

    def add_rule(self, rule: RiskRule) -> None:
        """Register an active risk rule."""
        self.rules.append(rule)

    def evaluate(self, intent: Intent, view: RiskViewContext) -> RiskDecision:
        """Evaluate an intent against safety controls and all loaded risk rules."""
        intent_id = getattr(intent, "intent_id", "")

        # (a) Kill-switch check
        if self.killswitch.is_active():
            ks_status = self.killswitch.status()
            reason = ks_status.get("reason", "Kill-switch engaged")
            decision = RiskDecision(
                allow=False,
                rule="killswitch",
                reason=f"Kill-switch active: {reason}",
                values={"active": True, "reason": reason},
            )
            self._journal_decision(intent_id, decision)
            return decision

        # CancelOrder does not require market quote or trading session check
        if isinstance(intent, CancelOrder):
            decision = RiskDecision(
                allow=True, rule="cancel_order", reason="Cancel order approved", values={}
            )
            self._journal_decision(intent_id, decision)
            return decision

        if not isinstance(intent, PlaceOrder):
            decision = RiskDecision(
                allow=False,
                rule="invalid_intent",
                reason=f"Unsupported intent type {type(intent)}",
                values={},
            )
            self._journal_decision(intent_id, decision)
            return decision

        # (b) Data integrity checks
        spec = intent.spec
        inst_id = spec.instrument

        # 1. Instrument known
        if view.instruments and inst_id not in view.instruments:
            decision = RiskDecision(
                allow=False,
                rule="data_integrity.unknown_instrument",
                reason=f"Unknown instrument: {inst_id}",
                values={"instrument": str(inst_id)},
            )
            self._journal_decision(intent_id, decision)
            return decision

        # 2. Quote present
        quote = view.quote_for(inst_id)
        if quote is None:
            decision = RiskDecision(
                allow=False,
                rule="data_integrity.missing_quote",
                reason=f"Missing market quote for {inst_id}",
                values={"instrument": str(inst_id)},
            )
            self._journal_decision(intent_id, decision)
            return decision

        # 3. Quote staleness
        profile = view.profile
        stale_key = f"risk.{profile}.stale_data_seconds"
        stale_limit_s: float | None = None
        if hasattr(view.params, "get"):
            try:
                param_val: Any = view.params.get(stale_key)
                if param_val is not None:
                    stale_limit_s = float(getattr(param_val, "value", param_val))
            except Exception:
                stale_limit_s = None

        if stale_limit_s is None:
            # Fail closed on missing parameter
            decision = RiskDecision(
                allow=False,
                rule="fail_closed.missing_param",
                reason=f"Missing required parameter '{stale_key}'",
                values={"param": stale_key},
            )
            self._journal_decision(intent_id, decision)
            return decision

        now_ts = view.clock.now()
        age_s = (now_ts - quote.recv_ts).total_seconds()
        if age_s > stale_limit_s:
            decision = RiskDecision(
                allow=False,
                rule="data_integrity.stale_data",
                reason=f"Quote age {age_s:.2f}s exceeds stale limit {stale_limit_s}s",
                values={"age_s": age_s, "limit_s": stale_limit_s},
            )
            self._journal_decision(intent_id, decision)
            return decision

        # 4. Entry allowed by SessionManager
        reducing = is_reducing_intent(intent, view)
        if not reducing and view.session_manager is not None:
            if hasattr(view.session_manager, "trading_allowed"):
                if not view.session_manager.trading_allowed(inst_id, now_ts):
                    decision = RiskDecision(
                        allow=False,
                        rule="data_integrity.session_closed",
                        reason=f"Market session closed for {inst_id}",
                        values={"instrument": str(inst_id)},
                    )
                    self._journal_decision(intent_id, decision)
                    return decision

        # (c) Run ALL rules of all loaded risk-rule plugins (NO SHORT-CIRCUIT)
        all_decisions: list[RiskDecision] = []

        for rule in self.rules:
            # Check rule name
            rule_name = getattr(rule, "name", str(type(rule).__name__))
            # (d) reducing intents skip entry-only rules
            if reducing and rule_name in ENTRY_ONLY_RULES:
                continue

            try:
                view_arg: Any = view
                dec = rule.check(intent, view_arg)
                all_decisions.append(dec)
            except Exception as exc:
                # Fail closed on exception during rule execution
                all_decisions.append(
                    RiskDecision(
                        allow=False,
                        rule=rule_name,
                        reason=f"Exception in rule {rule_name}: {exc}",
                        values={"error": str(exc)},
                    )
                )

        # Merge decisions: all compared numbers merged into one decision
        merged_values: dict[str, Any] = {}
        breached_rules: list[str] = []
        breached_reasons: list[str] = []

        for dec in all_decisions:
            merged_values.update(dec.values)
            if not dec.allow:
                breached_rules.append(dec.rule)
                breached_reasons.append(dec.reason)

        if breached_rules:
            merged_decision = RiskDecision(
                allow=False,
                rule=", ".join(breached_rules),
                reason="; ".join(breached_reasons),
                values=merged_values,
            )
        else:
            merged_decision = RiskDecision(
                allow=True,
                rule="all_rules_passed",
                reason="All risk rules approved",
                values=merged_values,
            )

        if self.observe and not reducing:
            merged_decision = RiskDecision(
                allow=False,
                rule="observe_mode",
                reason="observe mode",
                values=merged_values,
            )

        self._journal_decision(intent_id, merged_decision)
        return merged_decision

    def _journal_decision(self, intent_id: str, decision: RiskDecision) -> None:
        """Record risk.decision in journal."""
        if not decision.allow:
            for r in [x.strip() for x in str(decision.rule).split(",") if x.strip()]:
                self.rejects_by_rule[r] = self.rejects_by_rule.get(r, 0) + 1
        if self.journal is not None and hasattr(self.journal, "append"):
            self.journal.append(
                "risk.decision",
                {
                    "intent_id": intent_id,
                    "allow": decision.allow,
                    "rule": decision.rule,
                    "reason": decision.reason,
                    "values": {k: str(v) for k, v in decision.values.items()},
                },
            )
