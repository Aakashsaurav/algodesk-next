"""Tests for environment variable secrets provider plugin."""

from __future__ import annotations

from typing import Any

import pytest
from plugins.secrets.env.plugin import EnvSecretsPlugin

from algodesk.api import ConfigError, NotSupported, PluginManifest, secrets_problems
from algodesk.api.kinds import Mode
from algodesk.ops.context import build_context


@pytest.fixture
def fake_manifest() -> PluginManifest:
    return PluginManifest(
        name="env",
        kind="secrets",
        version="0.1.0",
        api_version=1,
        entry="plugin:EnvSecretsPlugin",
        capabilities={"read": True, "write": False, "delete": False, "env": True},
        modes_allowed=["research", "backtest", "paper", "live"],
    )


def test_env_provider_get(fake_manifest: PluginManifest, monkeypatch: Any) -> None:
    monkeypatch.setenv("ALGODESK_SECRET_UPSTOX_TOKEN", "test-token-val-456")  # fake-secret
    ctx = build_context(Mode.RESEARCH)
    provider = EnvSecretsPlugin(fake_manifest, {}, ctx)

    val = provider.get("upstox_token")
    assert val == "test-token-val-456"

    val_upper = provider.get("UPSTOX_TOKEN")
    assert val_upper == "test-token-val-456"


def test_env_provider_missing_raises_config_error(
    fake_manifest: PluginManifest, monkeypatch: Any
) -> None:
    monkeypatch.delenv("ALGODESK_SECRET_MISSING_SECRET", raising=False)
    ctx = build_context(Mode.RESEARCH)
    provider = EnvSecretsPlugin(fake_manifest, {}, ctx)

    with pytest.raises(ConfigError) as exc_info:
        provider.get("missing_secret")
    assert "Secret 'missing_secret' not found in environment" in exc_info.value.what


def test_env_provider_names(fake_manifest: PluginManifest, monkeypatch: Any) -> None:
    monkeypatch.setenv("ALGODESK_SECRET_ALPHA", "val-a")  # fake-secret
    monkeypatch.setenv("ALGODESK_SECRET_BETA", "val-b")  # fake-secret
    ctx = build_context(Mode.RESEARCH)
    provider = EnvSecretsPlugin(fake_manifest, {}, ctx)

    names = provider.names()
    assert "alpha" in names
    assert "beta" in names


def test_env_provider_set_delete_not_supported(fake_manifest: PluginManifest) -> None:
    ctx = build_context(Mode.RESEARCH)
    provider = EnvSecretsPlugin(fake_manifest, {}, ctx)

    with pytest.raises(NotSupported):
        provider.set("key", "val")  # fake-secret

    with pytest.raises(NotSupported):
        provider.delete("key")


def test_env_provider_conformance(fake_manifest: PluginManifest) -> None:
    ctx = build_context(Mode.RESEARCH)
    provider = EnvSecretsPlugin(fake_manifest, {}, ctx)
    problems = secrets_problems(provider)
    assert problems == []
