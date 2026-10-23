"""Configuration schema for exchange calendar plugin."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExchangeCalendarConfig(BaseModel):
    """Configuration options for the exchange calendar plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exchange: Literal["nse", "bse", "mcx"] = Field(
        default="nse",
        description="Exchange identifier (nse, bse, or mcx)",
    )


ExchangeCalendarConfig.model_rebuild()
