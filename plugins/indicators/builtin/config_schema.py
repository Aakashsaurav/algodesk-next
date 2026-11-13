"""Configuration schema for builtin indicators plugin."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BuiltinIndicatorsConfig(BaseModel):
    """Configuration options for builtin indicators plugin."""

    model_config = ConfigDict(extra="allow", frozen=True)

    indicator: str = Field(default="sma", description="Indicator name")


BuiltinIndicatorsConfig.model_rebuild()
