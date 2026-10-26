"""Tests for EnvSecretsPlugin."""

from __future__ import annotations

import os

import pytest

from algodesk.api import ConfigError, NotSupported, PluginManifest, secrets_problems

from ..plugin import EnvSecretsPlugin


@pytest.fixture
def manifest() -> PluginManifest:
    return PluginManifest(
        name="env",
        kind="secrets",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:EnvSecretsPlugin",
        capabilities={"env": True, "read": True, "write": False, "delete": False},
    )


def test_env_secrets_get(manifest: PluginManifest, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALGODESK_SECRET_TEST_TOKEN", "super-secret-token")  # fake-secret
    plugin = EnvSecretsPlugin(manifest)

    val = plugin.get("test_token")
    assert val == "super-secret-token"


def test_env_secrets_missing_raises_config_error(manifest: PluginManifest) -> None:
    plugin = EnvSecretsPlugin(manifest)
    with pytest.raises(ConfigError) as exc_info:
        plugin.get("nonexistent_secret_key")
    assert "nonexistent_secret_key" in exc_info.value.what
    assert "ALGODESK_SECRET_NONEXISTENT_SECRET_KEY" in exc_info.value.fix


def test_env_secrets_set_raises_not_supported(manifest: PluginManifest) -> None:
    plugin = EnvSecretsPlugin(manifest)
    with pytest.raises(NotSupported, match="not supported"):
        plugin.set("any_key", "any_value")


def test_env_secrets_delete_raises_not_supported(manifest: PluginManifest) -> None:
    plugin = EnvSecretsPlugin(manifest)
    with pytest.raises(NotSupported, match="not supported"):
        plugin.delete("any_key")


def test_env_secrets_names(manifest: PluginManifest, monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear any leftover ALGODESK_SECRET_* from env
    for k in list(os.environ.keys()):
        if k.startswith("ALGODESK_SECRET_"):
            monkeypatch.delenv(k, raising=False)

    monkeypatch.setenv("ALGODESK_SECRET_FOO", "v1")  # fake-secret
    monkeypatch.setenv("ALGODESK_SECRET_BAR", "v2")  # fake-secret
    monkeypatch.setenv("OTHER_VAR", "ignored")

    plugin = EnvSecretsPlugin(manifest)
    assert sorted(plugin.names()) == ["bar", "foo"]


def test_env_secrets_conformance(manifest: PluginManifest) -> None:
    plugin = EnvSecretsPlugin(manifest)
    # secrets_problems checks names() and missing get() raising ConfigError
    problems = secrets_problems(plugin)
    assert problems == []
