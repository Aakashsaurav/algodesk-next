"""Builtin risk rules plugin exposing standard pre-trade risk checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from algodesk.api import Intent, PluginBase, PluginManifest, RiskDecision

from .rules import (
    CashAvailableRule,
    DailyLossLimitRule,
    MaxDailyTradesRule,
    MaxGrossExposureRule,
    MaxOpenPositionsRule,
    MaxOrdersPerMinuteRule,
    MaxOrderValueRule,
    MaxPositionRule,
    PriceDeviationRule,
)


class BuiltinRiskRulesPlugin(PluginBase):
    """Risk rule plugin orchestrating standard pre-trade checks."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, object] | None = None,
        ctx: Any = None,
    ) -> None:
        super().__init__(manifest, config or {}, ctx)
        self.rules = [
            CashAvailableRule(),
            MaxOrderValueRule(),
            MaxPositionRule(),
            MaxOpenPositionsRule(),
            MaxGrossExposureRule(),
            PriceDeviationRule(),
            MaxOrdersPerMinuteRule(),
            MaxDailyTradesRule(),
            DailyLossLimitRule(),
        ]

    def check(self, intent: Intent, view: Any) -> RiskDecision:
        """Run all active builtin rules and merge into one decision."""
        breached_rules: list[str] = []
        breached_reasons: list[str] = []
        merged_values: dict[str, Any] = {}

        for rule in self.rules:
            dec = rule.check(intent, view)
            merged_values.update(dec.values)
            if not dec.allow:
                breached_rules.append(dec.rule)
                breached_reasons.append(dec.reason)

        if breached_rules:
            return RiskDecision(
                allow=False,
                rule=", ".join(breached_rules),
                reason="; ".join(breached_reasons),
                values=merged_values,
            )

        return RiskDecision(
            allow=True,
            rule="builtin_rules_passed",
            reason="All builtin risk rules passed",
            values=merged_values,
        )


__all__ = [
    "BuiltinRiskRulesPlugin",
    "CashAvailableRule",
    "DailyLossLimitRule",
    "MaxDailyTradesRule",
    "MaxGrossExposureRule",
    "MaxOpenPositionsRule",
    "MaxOrdersPerMinuteRule",
    "MaxOrderValueRule",
    "MaxPositionRule",
    "PriceDeviationRule",
]
