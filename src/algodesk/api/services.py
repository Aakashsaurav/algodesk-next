"""Service interfaces and runtime context for AlgoDesk plugins."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from algodesk.api.kinds import Mode

type ParamValue = bool | int | float | str


@runtime_checkable
class ParamReader(Protocol):
    """Service for reading parameters from the parameter registry."""

    def get(self, name: str, scope: str | None = None) -> ParamValue:
        """Return parameter value by name and optional scope."""
        ...


@runtime_checkable
class JournalWriter(Protocol):
    """Service for appending events to the append-only journal."""

    def append(
        self,
        kind: str,
        payload: Mapping[str, object],
        *,
        durable: bool = False,
    ) -> int:
        """Append an event to the journal and return the sequence number."""
        ...


@runtime_checkable
class SecretReader(Protocol):
    """Service for resolving secret references.

    Raises
    ------
    ConfigError
        If the secret is missing. The secret value must never be logged.
    """

    def get(self, name: str) -> str:
        """Return the secret string for *name*."""
        ...


@dataclass(frozen=True)
class PluginContext:
    """Runtime context bundled for plugin instantiation."""

    mode: Mode
    run_id: str
    params: ParamReader
    journal: JournalWriter
    secrets: SecretReader
