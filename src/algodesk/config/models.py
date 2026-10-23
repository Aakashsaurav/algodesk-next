"""Configuration models for platform and run configurations (pydantic v2)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from algodesk.api.kinds import Mode

_LOG_LEVELS: set[str] = {"DEBUG", "INFO", "WARNING", "ERROR"}
_LOG_FORMATS: set[str] = {"json", "text"}


class PathsConfig(BaseModel):
    """File system paths for data, logs, results, and local workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: str = "data"
    logs_dir: str = "logs"
    results_dir: str = "results"
    local_dir: str = ".local"

    @field_validator("data_dir", "logs_dir", "results_dir", "local_dir")
    @classmethod
    def _validate_relative_path(cls, v: str) -> str:
        p = Path(v)
        if p.is_absolute() or ".." in p.parts:
            msg = f"Path '{v}' must be a relative path without traversal ('..')"
            raise ValueError(msg)
        return v


class LoggingConfig(BaseModel):
    """Logging settings for the platform."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: str = "INFO"
    format: str = "json"

    @field_validator("level")
    @classmethod
    def _validate_level(cls, v: str) -> str:
        upper = v.upper()
        if upper not in _LOG_LEVELS:
            msg = f"Invalid log level '{v}'. Allowed: {', '.join(sorted(_LOG_LEVELS))}"
            raise ValueError(msg)
        return upper

    @field_validator("format")
    @classmethod
    def _validate_format(cls, v: str) -> str:
        lower = v.lower()
        if lower not in _LOG_FORMATS:
            msg = f"Invalid log format '{v}'. Allowed: {', '.join(sorted(_LOG_FORMATS))}"
            raise ValueError(msg)
        return lower


class PlatformConfig(BaseModel):
    """Platform-level configuration loaded from config/platform.toml and profiles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project: str = "algodesk"
    paths: PathsConfig = Field(default_factory=PathsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    plugin_dirs: list[str] = Field(default_factory=lambda: ["plugins"])


class PluginRef(BaseModel):
    """Reference to a plugin by name, accepting arbitrary plugin-specific options."""

    model_config = ConfigDict(extra="allow", frozen=True)

    plugin: str


class RunMeta(BaseModel):
    """Metadata identifying the run name and execution mode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    mode: Mode


class FeesConfig(BaseModel):
    """Fee schedule configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schedule: str


class DeploymentConfig(BaseModel):
    """Deployment target profile configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str = "local"


class UniverseConfig(BaseModel):
    """Universe definition, specifying exactly one of screen, symbols, or group."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    screen: str | None = None
    symbols: list[str] | None = None
    group: str | None = None

    @model_validator(mode="after")
    def _validate_universe(self) -> UniverseConfig:
        has_screen = self.screen is not None and bool(self.screen.strip())
        has_symbols = self.symbols is not None and len(self.symbols) > 0
        has_group = self.group is not None and bool(self.group.strip())
        count = sum([bool(has_screen), bool(has_symbols), bool(has_group)])
        if count != 1:
            msg = (
                "Universe requires exactly one of 'screen', 'symbols', or 'group' to be set; "
                f"got screen={self.screen!r}, symbols={self.symbols!r}, group={self.group!r}"
            )
            raise ValueError(msg)
        return self


class StrategyConfig(PluginRef):
    """Strategy configuration with risk profile and strategy parameters."""

    model_config = ConfigDict(extra="allow", frozen=True)

    risk_profile: str
    params: dict[str, Any] = Field(default_factory=dict)


class RunConfig(BaseModel):
    """Complete run configuration assembled from layered TOML files."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allocated_capital: Decimal
    run: RunMeta
    data_source: PluginRef
    execution_venue: PluginRef
    calendars: list[PluginRef]
    secrets: PluginRef
    fees: FeesConfig
    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)
    universe: UniverseConfig
    strategies: list[StrategyConfig]
    notifiers: list[PluginRef] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_calendars(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "calendars" not in data and "calendar" in data:
                cal = data.pop("calendar")
                if isinstance(cal, list):
                    data["calendars"] = cal
                elif isinstance(cal, dict):
                    data["calendars"] = [cal]
        return data

    @field_validator("allocated_capital")
    @classmethod
    def _validate_allocated_capital(cls, v: Decimal) -> Decimal:
        if v <= 0:
            msg = "allocated_capital must be greater than 0"
            raise ValueError(msg)
        return v

    @field_validator("calendars")
    @classmethod
    def _validate_calendars(cls, v: list[PluginRef]) -> list[PluginRef]:
        if not v:
            msg = "At least one calendar must be defined under [[calendars]]"
            raise ValueError(msg)
        return v

    @field_validator("strategies")
    @classmethod
    def _validate_strategies(cls, v: list[StrategyConfig]) -> list[StrategyConfig]:
        if not v:
            msg = "At least one strategy must be defined under [[strategies]]"
            raise ValueError(msg)
        return v
