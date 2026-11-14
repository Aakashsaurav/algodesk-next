"""Screen engine package: TOML rule screens, feature extraction, ranking and runner."""

from __future__ import annotations

from .features import InMemoryFeatureView, InMemorySymbolFeatureView, build_feature_view
from .models import (
    GroupRule,
    OperandModel,
    RankSpec,
    RequireRule,
    TomlScreenModel,
    load_toml_screen,
)
from .ranking import apply_ranking, compute_percentile_ranks
from .rules import RuleEvaluationError, eval_group_rule, eval_require_rule, eval_screen_rules
from .runner import ScreenRunner, compute_result_hash

__all__ = [
    "GroupRule",
    "InMemoryFeatureView",
    "InMemorySymbolFeatureView",
    "OperandModel",
    "RankSpec",
    "RequireRule",
    "RuleEvaluationError",
    "ScreenRunner",
    "TomlScreenModel",
    "apply_ranking",
    "build_feature_view",
    "compute_percentile_ranks",
    "compute_result_hash",
    "eval_group_rule",
    "eval_require_rule",
    "eval_screen_rules",
    "load_toml_screen",
]
