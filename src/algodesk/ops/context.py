"""Default runtime services and PluginContext construction."""

from __future__ import annotations

import threading
from collections.abc import Mapping

from algodesk.api.errors import ConfigError, ParamError
from algodesk.api.kinds import Mode
from algodesk.api.services import (
    JournalWriter,
    ParamReader,
    ParamValue,
    PluginContext,
    SecretReader,
)


class NullJournal:
    """Discards events and returns sequentially increasing sequence numbers."""

    def __init__(self, initial_seq: int = 1) -> None:
        self._seq = initial_seq - 1
        self._lock = threading.Lock()

    def append(
        self,
        kind: str,
        payload: Mapping[str, object],
        *,
        durable: bool = False,
    ) -> int:
        """Discard event and return next incremented sequence number."""
        with self._lock:
            self._seq += 1
            return self._seq


class DenySecrets:
    """Secret reader that denies access because no provider is configured."""

    def get(self, name: str) -> str:
        """Raise ConfigError indicating secrets provider is not configured."""
        raise ConfigError(
            what=f"no secrets provider is configured yet: {name}",
            where=f"secret:{name}",
            fix="Configure a secrets provider before accessing secrets",
        )


class EmptyParamReader:
    """Empty parameter reader that raises ParamError on access."""

    def get(self, name: str, scope: str | None = None) -> ParamValue:
        """Raise ParamError for any parameter request."""
        raise ParamError(
            what=f"Parameter '{name}' not found: no parameter registry configured",
            where=name,
            fix="Initialize runtime with parameter registry before reading parameters",
        )


def build_context(
    mode: Mode,
    run_id: str = "default",
    *,
    params: ParamReader | None = None,
    journal: JournalWriter | None = None,
    secrets: SecretReader | None = None,
) -> PluginContext:
    """Build a PluginContext with mode and services, defaulting to null implementations."""
    return PluginContext(
        mode=mode,
        run_id=run_id,
        params=params if params is not None else EmptyParamReader(),
        journal=journal if journal is not None else NullJournal(),
        secrets=secrets if secrets is not None else DenySecrets(),
    )
