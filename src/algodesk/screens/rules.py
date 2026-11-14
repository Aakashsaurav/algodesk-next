"""Rule evaluator for TOML rule screens."""

from __future__ import annotations

from typing import Any

from algodesk.api.screens import ScreenRow, SkipReason, SymbolFeatureView
from algodesk.screens.models import GroupRule, OperandModel, RequireRule, TomlScreenModel


class RuleEvaluationError(Exception):
    """Raised when an operand cannot be evaluated due to missing history or indicator error."""

    def __init__(self, reason: SkipReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _eval_operand(operand: OperandModel, view: SymbolFeatureView, offset: int = 0) -> float | None:
    bars_ago = operand.bars_ago + offset
    if operand.is_field:
        assert operand.field is not None
        return view.get_bar(operand.field, bars_ago=bars_ago)

    if operand.is_indicator:
        assert operand.ind is not None
        return view.get_indicator(
            operand.ind,
            output=operand.output,
            bars_ago=bars_ago,
            **operand.params,
        )

    if operand.is_const:
        assert operand.const is not None
        return float(operand.const)

    return None


def _format_operand_name(operand: OperandModel) -> str:
    if operand.is_field:
        return (
            f"{operand.field}" if operand.bars_ago == 0 else f"{operand.field}_{operand.bars_ago}"
        )
    if operand.is_indicator:
        ind_str = f"{operand.ind}"
        for _, v in sorted(operand.params.items()):
            ind_str += f"_{v}"
        if operand.output and operand.output != "value":
            ind_str += f"_{operand.output}"
        if operand.bars_ago > 0:
            ind_str += f"_{operand.bars_ago}"
        return ind_str
    if operand.is_const:
        return f"const_{operand.const}"
    return "operand"


def _resolve_operand(op_val: OperandModel | float | int | dict[str, Any]) -> OperandModel:
    if isinstance(op_val, OperandModel):
        return op_val
    if isinstance(op_val, (int, float)):
        return OperandModel(const=float(op_val))
    if isinstance(op_val, dict):
        return OperandModel(**op_val)
    raise ValueError(f"Cannot resolve operand from: {op_val}")


def eval_require_rule(
    rule: RequireRule,
    view: SymbolFeatureView,
    recorded_values: dict[str, float | None],
) -> bool:
    """Evaluate a single require rule, recording evaluated values."""
    op = rule.op
    left_name = _format_operand_name(rule.left)
    val_left = _eval_operand(rule.left, view, 0)
    recorded_values[left_name] = val_left

    if val_left is None:
        raise RuleEvaluationError(
            SkipReason.INSUFFICIENT_HISTORY,
            f"Operand '{left_name}' returned None (insufficient bar history)",
        )

    if op in (">", "<", ">=", "<="):
        right_op = _resolve_operand(rule.right)
        right_name = _format_operand_name(right_op)
        val_right = _eval_operand(right_op, view, 0)
        recorded_values[right_name] = val_right
        if val_right is None:
            raise RuleEvaluationError(
                SkipReason.INSUFFICIENT_HISTORY,
                f"Operand '{right_name}' returned None (insufficient bar history)",
            )

        if op == ">":
            return val_left > val_right
        if op == "<":
            return val_left < val_right
        if op == ">=":
            return val_left >= val_right
        if op == "<=":
            return val_left <= val_right

    if op == "between":
        right_op = _resolve_operand(rule.right)
        low_val: float | None = None
        if isinstance(right_op.low, (int, float)):
            low_val = float(right_op.low)
        elif isinstance(right_op.low, dict):
            low_val = _eval_operand(OperandModel(**right_op.low), view, 0)
        elif isinstance(right_op.low, OperandModel):
            low_val = _eval_operand(right_op.low, view, 0)

        high_val: float | None = None
        if isinstance(right_op.high, (int, float)):
            high_val = float(right_op.high)
        elif isinstance(right_op.high, dict):
            high_val = _eval_operand(OperandModel(**right_op.high), view, 0)
        elif isinstance(right_op.high, OperandModel):
            high_val = _eval_operand(right_op.high, view, 0)

        recorded_values[f"{left_name}_between_low"] = low_val
        recorded_values[f"{left_name}_between_high"] = high_val

        if low_val is None or high_val is None:
            raise RuleEvaluationError(
                SkipReason.INSUFFICIENT_HISTORY,
                f"Between bounds evaluated to None for '{left_name}'",
            )
        return low_val <= val_left <= high_val

    if op in ("crosses_above", "crosses_below"):
        right_op = _resolve_operand(rule.right)
        right_name = _format_operand_name(right_op)
        val_right = _eval_operand(right_op, view, 0)
        recorded_values[right_name] = val_right
        if val_right is None:
            raise RuleEvaluationError(
                SkipReason.INSUFFICIENT_HISTORY,
                f"Operand '{right_name}' returned None (insufficient bar history)",
            )

        # Check within bars
        within = rule.within
        for k in range(within):
            l_curr = _eval_operand(rule.left, view, k)
            r_curr = _eval_operand(right_op, view, k)
            l_prev = _eval_operand(rule.left, view, k + 1)
            r_prev = _eval_operand(right_op, view, k + 1)

            if l_curr is None or r_curr is None or l_prev is None or r_prev is None:
                raise RuleEvaluationError(
                    SkipReason.INSUFFICIENT_HISTORY,
                    f"Crossover check at offset {k} had insufficient history",
                )

            if op == "crosses_above" and (l_curr > r_curr and l_prev <= r_prev):
                return True
            if op == "crosses_below" and (l_curr < r_curr and l_prev >= r_prev):
                return True

        return False

    return False


def eval_group_rule(
    group: GroupRule,
    view: SymbolFeatureView,
    recorded_values: dict[str, float | None],
) -> bool:
    """Evaluate a composite group rule."""
    results: list[bool] = []
    for rule in group.require:
        res = eval_require_rule(rule, view, recorded_values)
        results.append(res)

    if group.mode == "any":
        return any(results)
    if group.mode == "not":
        return not any(results)
    if group.mode == "at_least":
        assert group.n is not None
        return sum(1 for r in results if r) >= group.n

    return False


def eval_screen_rules(
    screen: TomlScreenModel,
    view: SymbolFeatureView,
) -> ScreenRow:
    """Evaluate all requirements and groups for a symbol and return ScreenRow."""
    recorded_values: dict[str, float | None] = {}
    passed = True

    for req in screen.require:
        if not eval_require_rule(req, view, recorded_values):
            passed = False

    for grp in screen.group:
        if not eval_group_rule(grp, view, recorded_values):
            passed = False

    return ScreenRow(
        symbol=view.symbol,
        passed=passed,
        score=100.0 if passed else 0.0,
        values=recorded_values,
    )
