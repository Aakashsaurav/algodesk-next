"""Configuration schema for macOS Keychain secrets plugin."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class KeychainSecretsConfig(BaseModel):
    """Configuration options for macOS Keychain secrets provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str = Field(
        default="algodesk",
        description="macOS Keychain service name",
    )
    index_file: str | None = Field(
        default=None,
        description="Optional path to secrets index file under .algodesk",
    )


KeychainSecretsConfig.model_rebuild()
