"""Service interfaces and runtime context for AlgoDesk plugins."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from algodesk.api.errors import ConfigError
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


def resolve_secret_ref(value: str, reader: SecretReader | Any) -> str:
    """Resolve a 'secret:<name>' reference using the given secret reader.

    Raises
    ------
    ConfigError
        If the value does not begin with 'secret:' or if the secret cannot be read.
    """
    if not isinstance(value, str) or not value.startswith("secret:"):
        raise ConfigError(
            what=f"Invalid secret reference: '{value}'. Expected 'secret:<name>'",
            where=str(value),
            fix="Specify secret references in the format 'secret:<name>'",
        )
    name = value[len("secret:") :].strip()
    if not name:
        raise ConfigError(
            what="Empty secret name in reference",
            where=value,
            fix="Specify secret name after 'secret:' prefix (e.g. 'secret:upstox_access_token')",
        )
    return str(reader.get(name))


@dataclass(frozen=True)
class PluginContext:
    """Runtime context bundled for plugin instantiation."""

    mode: Mode
    run_id: str
    params: ParamReader
    journal: JournalWriter
    secrets: SecretReader
    scrubber: Any = None
