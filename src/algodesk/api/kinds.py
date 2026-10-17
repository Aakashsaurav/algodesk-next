"""Plugin kinds, run modes and the API version constant."""

from __future__ import annotations

from enum import StrEnum

API_VERSION: int = 1
"""Current API version. Plugins must declare this in their manifest."""


class PluginKind(StrEnum):
    """Every recognised plugin kind."""

    BROKER = "broker"
    VENUE = "venue"
    FEES = "fees"
    INDICATOR = "indicator"
    SCREEN = "screen"
    STRATEGY = "strategy"
    RISK_RULE = "risk_rule"
    NOTIFIER = "notifier"
    CALENDAR = "calendar"
    SECRETS = "secrets"
    TRANSPORT = "transport"
    MODEL = "model"
    REPORT = "report"


class Mode(StrEnum):
    """Execution modes, ordered from safest to riskiest."""

    RESEARCH = "research"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"
