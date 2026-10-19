"""Tests for ops/context.py — NullJournal, DenySecrets, build_context."""

from __future__ import annotations

import pytest

from algodesk.api.errors import ConfigError, ParamError
from algodesk.api.kinds import Mode
from algodesk.ops.context import DenySecrets, NullJournal, build_context


def test_null_journal_increments_sequence() -> None:
    """NullJournal discards events and returns sequential integers."""
    nj = NullJournal()
    assert nj.append("event.one", {"a": 1}) == 1
    assert nj.append("event.two", {"b": 2}, durable=True) == 2
    assert nj.append("event.three", {}) == 3


def test_deny_secrets_raises_config_error() -> None:
    """DenySecrets raises ConfigError identifying missing provider."""
    ds = DenySecrets()
    with pytest.raises(ConfigError) as exc_info:
        ds.get("kite_api_key")
    assert "no secrets provider is configured yet" in exc_info.value.what
    assert "secret:kite_api_key" in exc_info.value.where


def test_build_context_default_services() -> None:
    """build_context sets mode, run_id, and null fallback services."""
    ctx = build_context(Mode.RESEARCH, run_id="run-101")
    assert ctx.mode == Mode.RESEARCH
    assert ctx.run_id == "run-101"
    assert ctx.journal.append("kind", {}) == 1

    with pytest.raises(ConfigError):
        ctx.secrets.get("some_secret")

    with pytest.raises(ParamError):
        ctx.params.get("some.param")


def test_build_context_custom_services() -> None:
    """build_context accepts explicit services."""

    class MockParamReader:
        def get(self, name: str, scope: str | None = None) -> str:
            return "mock_val"

    ctx = build_context(
        Mode.PAPER,
        run_id="run-paper",
        params=MockParamReader(),
    )
    assert ctx.mode == Mode.PAPER
    assert ctx.params.get("any.param") == "mock_val"
