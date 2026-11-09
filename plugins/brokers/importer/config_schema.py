"""Configuration schema for vendor importer broker plugin."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ImporterConfig(BaseModel):
    """Configuration options for importer DataSource plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_exchange: str = Field(
        default="NSE",
        description="Default exchange to map bare symbols to (NSE or BSE)",
    )
    mapping_file: str | None = Field(
        default=None,
        description="Optional default path to column mapping TOML or JSON file",
    )


ImporterConfig.model_rebuild()

