"""Tests for secret references, resolution, and Scrubber wrapping."""

from __future__ import annotations

import pytest

from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.api.scrub import Scrubber
from algodesk.api.services import resolve_secret_ref
from algodesk.ops.context import build_context


class MockSecretReader:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = secrets or {}

    def get(self, name: str) -> str:
        if name not in self._secrets:
            raise ConfigError(
                what=f"Secret '{name}' not found",
                where=f"secret:{name}",
                fix="Set secret",
            )
        return self._secrets[name]

    def names(self) -> list[str]:
        return list(self._secrets.keys())


def test_resolve_secret_ref_valid() -> None:
    reader = MockSecretReader({"upstox_token": "my-secret-access-token"})  # fake-secret
    val = resolve_secret_ref("secret:upstox_token", reader)
    assert val == "my-secret-access-token"


def test_resolve_secret_ref_invalid_format() -> None:
    reader = MockSecretReader({"key": "val"})  # fake-secret
    with pytest.raises(ConfigError) as exc_info:
        resolve_secret_ref("raw_token_value", reader)
    assert "Expected 'secret:<name>'" in exc_info.value.what

    with pytest.raises(ConfigError) as exc_info2:
        resolve_secret_ref("secret:", reader)
    assert "Empty secret name" in exc_info2.value.what


def test_resolve_secret_ref_missing_secret() -> None:
    reader = MockSecretReader({})
    with pytest.raises(ConfigError, match="not found"):
        resolve_secret_ref("secret:missing_key", reader)


def test_build_context_wraps_secrets_reader_and_registers_scrubber() -> None:
    reader = MockSecretReader({"app_key": "long_secret_api_key_value_12345"})  # fake-secret
    scrubber = Scrubber()
    ctx = build_context(Mode.RESEARCH, secrets=reader, scrubber=scrubber)

    # Before retrieval, scrubber does not have it registered
    raw_text = "Call with key=long_secret_api_key_value_12345 payload"
    # When retrieved via context.secrets.get
    secret_val = ctx.secrets.get("app_key")
    assert secret_val == "long_secret_api_key_value_12345"

    # Now the scrubber has registered it and scrubs it
    scrubbed = scrubber.scrub(raw_text)
    assert "long_secret_api_key_value_12345" not in scrubbed
    assert "***" in scrubbed
