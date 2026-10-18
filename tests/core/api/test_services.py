"""Tests for algodesk.api.services — service protocols and PluginContext."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError

import pytest

from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.api.services import (
    JournalWriter,
    ParamReader,
    ParamValue,
    PluginContext,
    SecretReader,
)


class FakeParamReader:
    """Fake parameter registry reader for testing."""

    def __init__(self, values: dict[str, ParamValue] | None = None) -> None:
        self._values = values or {}

    def get(self, name: str, scope: str | None = None) -> ParamValue:
        key = f"{scope}:{name}" if scope else name
        return self._values.get(key, self._values.get(name, 0))


class FakeJournalWriter:
    """Fake journal writer recording events in memory."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object], bool]] = []

    def append(
        self,
        kind: str,
        payload: Mapping[str, object],
        *,
        durable: bool = False,
    ) -> int:
        self.events.append((kind, payload, durable))
        return len(self.events)


class FakeSecretReader:
    """Fake secret reader resolving from a dictionary."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = secrets or {}

    def get(self, name: str) -> str:
        if name not in self._secrets:
            raise ConfigError(
                what=f"Secret '{name}' not found",
                where="FakeSecretReader",
                fix=f"Configure secret '{name}' in secret store",
            )
        return self._secrets[name]


def test_param_reader_protocol() -> None:
    """FakeParamReader implements ParamReader protocol."""
    reader = FakeParamReader({"risk.daily_loss": 5000.0, "scope1:param_a": "scoped_val"})
    assert isinstance(reader, ParamReader)
    assert reader.get("risk.daily_loss") == 5000.0
    assert reader.get("param_a", scope="scope1") == "scoped_val"


def test_param_value_types() -> None:
    """ParamValue allows bool, int, float, str."""
    values: list[ParamValue] = [True, 42, 3.14, "active"]
    reader = FakeParamReader({"b": values[0], "i": values[1], "f": values[2], "s": values[3]})
    assert reader.get("b") is True
    assert reader.get("i") == 42
    assert reader.get("f") == 3.14
    assert reader.get("s") == "active"


def test_journal_writer_protocol() -> None:
    """FakeJournalWriter implements JournalWriter protocol."""
    writer = FakeJournalWriter()
    assert isinstance(writer, JournalWriter)

    seq1 = writer.append("order.new", {"order_id": "ORD1"}, durable=False)
    assert seq1 == 1
    seq2 = writer.append("order.fill", {"fill_id": "F1"}, durable=True)
    assert seq2 == 2
    assert len(writer.events) == 2
    assert writer.events[1] == ("order.fill", {"fill_id": "F1"}, True)


def test_secret_reader_protocol() -> None:
    """FakeSecretReader implements SecretReader protocol."""
    reader = FakeSecretReader({"zerodha_api_key": "secret:key123"})
    assert isinstance(reader, SecretReader)
    assert reader.get("zerodha_api_key") == "secret:key123"


def test_missing_secret_raises_config_error() -> None:
    """Missing secret raises ConfigError with what, where, fix."""
    reader = FakeSecretReader()
    with pytest.raises(ConfigError) as exc_info:
        reader.get("missing_token")

    err = exc_info.value
    assert "missing_token" in err.what
    assert err.where == "FakeSecretReader"
    assert "Configure secret" in err.fix


def test_plugin_context_creation() -> None:
    """PluginContext holds mode, run_id, params, journal, and secrets."""
    params = FakeParamReader()
    journal = FakeJournalWriter()
    secrets = FakeSecretReader()

    ctx = PluginContext(
        mode=Mode.BACKTEST,
        run_id="run-20261018-001",
        params=params,
        journal=journal,
        secrets=secrets,
    )
    assert ctx.mode is Mode.BACKTEST
    assert ctx.run_id == "run-20261018-001"
    assert ctx.params is params
    assert ctx.journal is journal
    assert ctx.secrets is secrets


def test_plugin_context_is_frozen() -> None:
    """PluginContext is immutable and cannot be modified."""
    ctx = PluginContext(
        mode=Mode.RESEARCH,
        run_id="run-001",
        params=FakeParamReader(),
        journal=FakeJournalWriter(),
        secrets=FakeSecretReader(),
    )
    with pytest.raises(FrozenInstanceError):
        ctx.mode = Mode.LIVE  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        ctx.run_id = "other"  # type: ignore[misc]
