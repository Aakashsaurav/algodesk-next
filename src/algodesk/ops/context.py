"""Default runtime services and PluginContext construction."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any

from algodesk.api.errors import ConfigError, ParamError
from algodesk.api.kinds import Mode
from algodesk.api.scrub import Scrubber
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


class ScrubbingSecretReader:
    """Wraps a SecretReader to register every retrieved secret with a Scrubber."""

    def __init__(self, inner: SecretReader, scrubber: Scrubber) -> None:
        self._inner = inner
        self._scrubber = scrubber

    def get(self, name: str) -> str:
        val = self._inner.get(name)
        if isinstance(val, str) and val:
            self._scrubber.register(val)
        return val

    def names(self) -> Sequence[str]:
        inner: Any = self._inner
        if hasattr(inner, "names") and callable(inner.names):
            return inner.names()  # type: ignore[no-any-return]
        return []

    def set(self, name: str, value: str) -> None:
        inner: Any = self._inner
        if hasattr(inner, "set") and callable(inner.set):
            inner.set(name, value)
        else:
            raise ConfigError(
                what="Configured secrets provider does not support set",
                where=name,
                fix="Configure a writable secrets provider",
            )

    def delete(self, name: str) -> None:
        inner: Any = self._inner
        if hasattr(inner, "delete") and callable(inner.delete):
            inner.delete(name)
        else:
            raise ConfigError(
                what="Configured secrets provider does not support delete",
                where=name,
                fix="Configure a writable secrets provider",
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
    scrubber: Scrubber | None = None,
) -> PluginContext:
    """Build a PluginContext with mode and services, defaulting to null implementations."""
    if scrubber is None:
        j_any: Any = journal
        if j_any is not None and hasattr(j_any, "scrubber") and j_any.scrubber is not None:
            scrubber = j_any.scrubber
        else:
            scrubber = Scrubber()

    raw_secrets = secrets if secrets is not None else DenySecrets()
    wrapped_secrets = ScrubbingSecretReader(raw_secrets, scrubber)

    return PluginContext(
        mode=mode,
        run_id=run_id,
        params=params if params is not None else EmptyParamReader(),
        journal=journal if journal is not None else NullJournal(),
        secrets=wrapped_secrets,
        scrubber=scrubber,
    )
