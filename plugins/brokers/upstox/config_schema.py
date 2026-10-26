"""Configuration schema for Upstox broker plugin."""

from __future__ import annotations

from pydantic import BaseModel, Field


class UpstoxConfig(BaseModel):
    """Configuration options for Upstox broker plugin."""

    api_key: str = Field(
        default="secret:upstox_client_id",
        description="Reference to Upstox API key / client ID",
    )
    api_secret: str = Field(
        default="secret:upstox_client_secret",
        description="Reference to Upstox API secret",
    )
    redirect_uri: str = Field(
        default="http://127.0.0.1:5000/",
        description="Redirect URI registered in Upstox Developer Console",
    )
    auth_priority: list[str] = Field(
        default_factory=lambda: ["analytics", "oauth"],
        description="Priority order of authentication tokens to attempt",
    )
    analytics_token: str = Field(
        default="secret:upstox_analytics_token",
        description="Reference to long-lived static analytics token",
    )
    analytics_expires: str = Field(
        default="secret:upstox_analytics_expires",
        description="Reference to analytics token expiry date",
    )
    access_token: str = Field(
        default="secret:upstox_access_token",
        description="Reference to OAuth access token",
    )
