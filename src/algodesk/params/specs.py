"""Parameter specifications, types, validation and CLI parsing."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from algodesk.api.errors import ParamError

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ParamType(StrEnum):
    """Supported parameter data types."""

    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STR = "str"
    ENUM = "enum"
    TIME = "time"


class Tighten(StrEnum):
    """Direction that makes a parameter limit stricter."""

    DOWN = "down"
    UP = "up"
    NONE = "none"


class ParamSpec(BaseModel):
    """Specification and bounds definition for a configuration parameter."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: ParamType
    default: Any
    min: float | int | None = None
    max: float | int | None = None
    choices: list[str] | None = None
    unit: str = ""
    scope: str = "global"
    tighten: Tighten = Tighten.NONE
    description: str = ""
    owner: str = "core"

    @model_validator(mode="before")
    @classmethod
    def _coerce_types(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "type" in data and isinstance(data["type"], str):
                try:
                    data["type"] = ParamType(data["type"])
                except ValueError:
                    name = data.get("name", "unknown")
                    raise ParamError(
                        what=f"Unknown parameter type '{data['type']}'",
                        where=name,
                        fix=f"Use one of: {[t.value for t in ParamType]}",
                    ) from None
            if "tighten" in data and isinstance(data["tighten"], str):
                try:
                    data["tighten"] = Tighten(data["tighten"])
                except ValueError:
                    name = data.get("name", "unknown")
                    raise ParamError(
                        what=f"Unknown tighten direction '{data['tighten']}'",
                        where=name,
                        fix=f"Use one of: {[t.value for t in Tighten]}",
                    ) from None
        return data

    @model_validator(mode="after")
    def _validate_spec(self) -> ParamSpec:
        if not NAME_PATTERN.match(self.name):
            raise ParamError(
                what=f"Invalid parameter name '{self.name}'",
                where=self.name,
                fix="Use lowercase dot-separated namespaces (e.g. 'risk.profile.limit')",
            )
        if self.type == ParamType.ENUM:
            if not self.choices or len(self.choices) == 0:
                raise ParamError(
                    what="Enum parameter requires non-empty choices",
                    where=self.name,
                    fix="Provide a non-empty list of string choices",
                )
        elif self.choices is not None:
            raise ParamError(
                what="Choices are only supported for enum parameters",
                where=self.name,
                fix="Remove choices or change type to enum",
            )
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ParamError(
                what=f"Minimum bound {self.min} exceeds maximum bound {self.max}",
                where=self.name,
                fix="Ensure min <= max",
            )
        valid_default = validate_value(self, self.default)
        object.__setattr__(self, "default", valid_default)
        return self


def _validate_type(spec: ParamSpec, value: Any) -> Any:
    """Validate and convert the type of a parameter value."""
    if spec.type == ParamType.BOOL:
        if not isinstance(value, bool):
            raise ParamError(
                what=f"Expected boolean, got {type(value).__name__} ({value!r})",
                where=spec.name,
                fix="Provide a boolean value (True or False)",
            )
        return value
    if spec.type == ParamType.INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ParamError(
                what=f"Expected integer, got {type(value).__name__} ({value!r})",
                where=spec.name,
                fix="Provide an integer (bool is not accepted as int)",
            )
        return value
    if spec.type == ParamType.FLOAT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParamError(
                what=f"Expected float or int, got {type(value).__name__} ({value!r})",
                where=spec.name,
                fix="Provide a float or int value",
            )
        return float(value)
    if spec.type == ParamType.STR:
        if not isinstance(value, str):
            raise ParamError(
                what=f"Expected string, got {type(value).__name__} ({value!r})",
                where=spec.name,
                fix="Provide a string value",
            )
        return value
    if spec.type == ParamType.ENUM:
        if not isinstance(value, str):
            raise ParamError(
                what=f"Expected string enum choice, got {type(value).__name__} ({value!r})",
                where=spec.name,
                fix="Provide a string choice",
            )
        choices = spec.choices or []
        if value not in choices:
            raise ParamError(
                what=f"Value '{value}' is not in choices {choices}",
                where=spec.name,
                fix=f"Choose one of: {choices}",
            )
        return value
    if spec.type == ParamType.TIME:
        if not isinstance(value, str) or not TIME_PATTERN.match(value):
            raise ParamError(
                what=f"Invalid time format '{value}'",
                where=spec.name,
                fix="Provide time in 'HH:MM' format between 00:00 and 23:59",
            )
        return value
    raise ParamError(
        what=f"Unsupported parameter type '{spec.type}'",
        where=spec.name,
        fix=f"Supported types are {[t.value for t in ParamType]}",
    )


def validate_value(spec: ParamSpec, value: Any) -> Any:
    """Validate a parameter value against spec type, bounds, and choices."""
    coerced = _validate_type(spec, value)
    if spec.type in (ParamType.INT, ParamType.FLOAT):
        if spec.min is not None and coerced < spec.min:
            raise ParamError(
                what=f"Value {coerced} is below minimum {spec.min}",
                where=spec.name,
                fix=f"Provide a value >= {spec.min}",
            )
        if spec.max is not None and coerced > spec.max:
            raise ParamError(
                what=f"Value {coerced} is above maximum {spec.max}",
                where=spec.name,
                fix=f"Provide a value <= {spec.max}",
            )
    return coerced


def parse_cli_value(spec: ParamSpec, raw: str) -> Any:
    """Parse a raw string from CLI input into a validated parameter value."""
    s = raw.strip()
    if spec.type == ParamType.BOOL:
        lower = s.lower()
        if lower in ("true", "t", "yes", "y", "1"):
            parsed: Any = True
        elif lower in ("false", "f", "no", "n", "0"):
            parsed = False
        else:
            raise ParamError(
                what=f"Cannot parse '{raw}' as boolean",
                where=spec.name,
                fix="Use 'true', 'false', 'yes', 'no', '1', or '0'",
            )
    elif spec.type == ParamType.INT:
        try:
            parsed = int(s)
        except ValueError:
            raise ParamError(
                what=f"Cannot parse '{raw}' as integer",
                where=spec.name,
                fix="Provide an integer string (e.g. '10')",
            ) from None
    elif spec.type == ParamType.FLOAT:
        try:
            parsed = float(s)
        except ValueError:
            raise ParamError(
                what=f"Cannot parse '{raw}' as float",
                where=spec.name,
                fix="Provide a numeric string (e.g. '1.5')",
            ) from None
    else:
        parsed = s

    return validate_value(spec, parsed)
