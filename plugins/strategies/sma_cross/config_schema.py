"""Configuration schema for SMA crossover strategy."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SMACrossConfig(BaseModel):
    """Parameters for the SMA crossover strategy."""

    model_config = ConfigDict(extra="ignore")

    instruments: list[str] = Field(default_factory=list)
    group: str | None = None
    timeframe: str = "5m"
    fast: int = Field(default=20, ge=1)
    slow: int = Field(default=50, ge=1)
    qty: int = Field(default=10, ge=1)
    product: str = "cnc"
    exit_time: str = "15:15"
    stop_pct: float | None = Field(default=None, ge=0.0, le=1.0)
