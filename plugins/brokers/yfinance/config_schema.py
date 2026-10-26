"""Configuration schema for yfinance broker plugin."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class YFinanceConfig(BaseModel):
    """Configuration options for yfinance DataSource plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_exchange: str = Field(
        default="NSE",
        description="Default exchange to map bare symbols to (NSE or BSE)",
    )


YFinanceConfig.model_rebuild()
