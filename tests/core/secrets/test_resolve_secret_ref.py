"""Tests for secret reference resolution and scrubber auto-registration."""

from __future__ import annotations

import pytest

from algodesk.api import ConfigError, Scrubber, resolve_secret_ref
from algodesk.api.kinds import Mode
from algodesk.ops.context import ScrubbingSecretReader, build_context


class DummySecretsProvider:
    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = dict(secrets)

    def get(self, name: str) -> str:
        if name in self._secrets:
            return self._secrets[name]
        raise ConfigError(
            what=f"Secret '{name}' not found",
            where="DummySecretsProvider.get",
            fix="Check configuration",
        )


@pytest.mark.parametrize(
    "secret_name,secret_val",
    [
        ("api_key", "val-key-123"),  # fake-secret
        ("auth_token", "tok-xyz-456"),  # fake-secret
        ("db_password", "pass-secret-789"),  # fake-secret
        ("client_secret", "sec-abc-000"),  # fake-secret
        ("pin", "1234"),  # fake-secret
        ("private_key", "priv-key-line"),  # fake-secret
        ("jwt_secret", "jwt-val-999"),  # fake-secret
        ("session_id", "sess-id-111"),  # fake-secret
        ("oauth_code", "oauth-code-222"),  # fake-secret
        ("custom_cred", "custom-cred-333"),  # fake-secret
    ],
)
def test_resolve_secret_ref_valid(secret_name: str, secret_val: str) -> None:
    provider = DummySecretsProvider({secret_name: secret_val})
    ref = f"secret:{secret_name}"
    resolved = resolve_secret_ref(ref, provider)
    assert resolved == secret_val


@pytest.mark.parametrize(
    "invalid_ref",
    [
        "plain_string_value",
        "secrets:foo",
        "secret",
        "env:MY_SECRET",
        "keychain:my_sec",
        "vault://secret",
        "http://secret",
        "Bearer abc123xyz",
        "123456",
        "",
    ],
)
def test_resolve_secret_ref_invalid_raises_config_error(invalid_ref: str) -> None:
    provider = DummySecretsProvider({"foo": "bar"})
    with pytest.raises(ConfigError) as exc_info:
        resolve_secret_ref(invalid_ref, provider)
    assert "Expected 'secret:<name>'" in exc_info.value.what


def test_resolve_secret_ref_missing_raises_config_error() -> None:
    provider = DummySecretsProvider({"existing": "val"})  # fake-secret
    with pytest.raises(ConfigError) as exc_info:
        resolve_secret_ref("secret:missing_item", provider)
    assert "Secret 'missing_item' not found" in exc_info.value.what


@pytest.mark.parametrize(
    "secret_val",
    [
        "secret-alpha-99",  # fake-secret
        "secret-beta-88",  # fake-secret
        "secret-gamma-77",  # fake-secret
        "secret-delta-66",  # fake-secret
        "secret-omega-55",  # fake-secret
    ],
)
def test_scrubber_auto_registration(secret_val: str) -> None:
    scrubber = Scrubber()
    provider = DummySecretsProvider({"my_secret": secret_val})
    reader = ScrubbingSecretReader(provider, scrubber)

    assert secret_val not in scrubber._registered
    val = reader.get("my_secret")
    assert val == secret_val
    assert secret_val in scrubber._registered

    # Text containing secret should now be scrubbed
    raw_log = f"User logged in with key {secret_val} successfully"
    scrubbed = scrubber.scrub(raw_log)
    assert secret_val not in scrubbed
    assert "***" in scrubbed


def test_build_context_wraps_secrets_in_scrubber() -> None:
    provider = DummySecretsProvider({"test_key": "hidden_credential_987"})  # fake-secret
    ctx = build_context(Mode.RESEARCH, secrets=provider)
    assert ctx.secrets is not None

    val = ctx.secrets.get("test_key")
    assert val == "hidden_credential_987"
    assert "hidden_credential_987" in ctx.scrubber._registered
