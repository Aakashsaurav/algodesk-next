"""Configuration schema for screen rotation strategy."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ScreenRotationConfig(BaseModel):
    """Parameters for the screen rotation strategy."""

    model_config = ConfigDict(extra="ignore")

    screen: str = Field(
        default="trend_rank_test", description="Name of screen to run at each rebalance"
    )
    group: str | None = Field(default=None, description="Optional instrument group")
    top_n: int = Field(default=5, ge=1, description="Number of top instruments to hold")
    weight_pct: float = Field(
        default=15.0, gt=0.0, le=100.0, description="Target percentage per position"
    )
    rebalance_every: int = Field(
        default=21, ge=1, description="Sessions between portfolio rebalances"
    )
    timeframe: str = Field(default="1d", description="Bar timeframe for rebalance trigger")
    product: str = Field(default="CNC", description="Trading product (CNC)")
