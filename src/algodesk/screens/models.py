"""Pydantic models and parser for TOML rule screens."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from algodesk.api.errors import ConfigError

ALLOWED_FIELDS = {"close", "open", "high", "low", "volume"}
ALLOWED_OPS = {">", "<", ">=", "<=", "between", "crosses_above", "crosses_below"}
ALLOWED_ADJUST = {"total", "splits", "none"}
ALLOWED_GROUP_MODES = {"any", "not", "at_least"}


class OperandModel(BaseModel):
    """Operand representing a price field, indicator value, constant, or range."""

    model_config = ConfigDict(extra="allow")

    field: str | None = None
    ind: str | None = None
    output: str | None = None
    const: float | int | None = None
    bars_ago: int = 0
    low: Any = None
    high: Any = None

    @model_validator(mode="after")
    def validate_operand(self) -> OperandModel:
        if self.field is not None:
            low_f = self.field.lower()
            if low_f not in ALLOWED_FIELDS:
                raise ValueError(
                    f"Invalid field '{self.field}'. Allowed fields: {sorted(ALLOWED_FIELDS)}"
                )
        elif self.ind is not None:
            if not self.ind.strip():
                raise ValueError("Indicator name 'ind' cannot be empty")
        elif self.const is not None:
            pass
        elif self.low is not None and self.high is not None:
            pass
        else:
            raise ValueError(
                "Operand must specify one of: 'field', 'ind', 'const', or ('low' and 'high')"
            )

        if self.bars_ago < 0:
            raise ValueError(f"bars_ago must be non-negative, got {self.bars_ago}")

        return self

    @property
    def is_field(self) -> bool:
        return self.field is not None

    @property
    def is_indicator(self) -> bool:
        return self.ind is not None

    @property
    def is_const(self) -> bool:
        return self.const is not None

    @property
    def is_range(self) -> bool:
        return self.low is not None and self.high is not None

    @property
    def params(self) -> dict[str, Any]:
        """Extra parameters for indicator operand."""
        standard = {"field", "ind", "output", "const", "bars_ago", "low", "high"}
        return {k: v for k, v in self.model_dump().items() if k not in standard and v is not None}


class RequireRule(BaseModel):
    """Single rule condition comparing left and right operands."""

    model_config = ConfigDict(extra="forbid")

    left: OperandModel
    op: str
    right: OperandModel | float | int | dict[str, Any]
    within: int = 1

    @model_validator(mode="after")
    def validate_rule(self) -> RequireRule:
        if self.op not in ALLOWED_OPS:
            raise ValueError(
                f"Invalid operator '{self.op}'. Allowed operators: {sorted(ALLOWED_OPS)}"
            )

        if self.op in ("crosses_above", "crosses_below") and self.within < 1:
            raise ValueError(f"Operator '{self.op}' requires within >= 1, got {self.within}")

        # Normalize right operand if passed as raw number or dict
        if isinstance(self.right, (int, float)):
            self.right = OperandModel(const=self.right)
        elif isinstance(self.right, dict):
            self.right = OperandModel(**self.right)

        if self.op == "between" and not getattr(self.right, "is_range", False):
            raise ValueError("Operator 'between' requires right operand with 'low' and 'high'")

        return self


class GroupRule(BaseModel):
    """Composite group rule with mode 'any', 'not', or 'at_least'."""

    model_config = ConfigDict(extra="forbid")

    mode: str
    n: int | None = None
    require: list[RequireRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_group(self) -> GroupRule:
        if self.mode not in ALLOWED_GROUP_MODES:
            raise ValueError(
                f"Invalid group mode '{self.mode}'. Allowed modes: {sorted(ALLOWED_GROUP_MODES)}"
            )

        if not self.require:
            raise ValueError(f"Group '{self.mode}' must have at least one nested require rule")

        if self.mode == "at_least":
            if self.n is None or self.n < 1:
                raise ValueError("Group mode 'at_least' requires positive integer 'n'")
            if self.n > len(self.require):
                raise ValueError(
                    f"Group mode 'at_least' n={self.n} exceeds number of "
                    f"nested rules ({len(self.require)})"
                )

        return self


class RankSpec(BaseModel):
    """Cross-sectional ranking specification across screened universe."""

    model_config = ConfigDict(extra="forbid")

    by: OperandModel
    method: Literal["percentile"] = "percentile"
    min: float | None = None

    @model_validator(mode="after")
    def validate_rank(self) -> RankSpec:
        if self.min is not None and not (0.0 <= self.min <= 100.0):
            raise ValueError(f"Rank min percentile must be between 0.0 and 100.0, got {self.min}")
        return self


class TomlScreenModel(BaseModel):
    """Root model for a TOML screen definition file."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    timeframe: str = "1d"
    adjust: str = "total"
    universe: str | None = None
    require: list[RequireRule] = Field(default_factory=list)
    group: list[GroupRule] = Field(default_factory=list)
    rank: RankSpec | None = None

    @model_validator(mode="after")
    def validate_screen(self) -> TomlScreenModel:
        if not self.name.strip():
            raise ValueError("Screen 'name' cannot be empty")

        if self.adjust not in ALLOWED_ADJUST:
            raise ValueError(
                f"Invalid adjust mode '{self.adjust}'. Allowed: {sorted(ALLOWED_ADJUST)}"
            )

        if not self.require and not self.group:
            raise ValueError("Screen must declare at least one [[require]] or [[group]] rule")

        return self


def load_toml_screen(path: Path | str) -> TomlScreenModel:
    """Load and strictly validate a TOML screen file, raising ConfigError with file and key."""
    p = Path(path).resolve()
    if not p.is_file():
        raise ConfigError(
            what=f"Screen file not found: {p}",
            where=str(p),
            fix="Check screen file path and name",
        )

    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            what=f"TOML parse syntax error: {exc}",
            where=f"{p}:syntax",
            fix="Correct syntax error in TOML screen file",
        ) from exc

    try:
        return TomlScreenModel.model_validate(data)
    except ValidationError as exc:
        # Extract first error details naming file and exact key
        first_err = exc.errors()[0]
        loc = ".".join(str(elem) for elem in first_err.get("loc", ()))
        msg = first_err.get("msg", "Invalid value")
        key_label = loc if loc else "root"
        raise ConfigError(
            what=f"Invalid screen configuration for '{key_label}': {msg}",
            where=f"{p}:{key_label}",
            fix=f"Review key '{key_label}' in {p.name}",
        ) from exc
