"""Tests for macOS Keychain secrets provider plugin."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from plugins.secrets.keychain.config_schema import KeychainSecretsConfig
from plugins.secrets.keychain.plugin import KeychainSecretsPlugin

from algodesk.api import ConfigError, PluginManifest, secrets_problems
from algodesk.api.kinds import Mode
from algodesk.ops.context import build_context


@pytest.fixture
def fake_manifest() -> PluginManifest:
    return PluginManifest(
        name="keychain",
        kind="secrets",
        version="0.1.0",
        api_version=1,
        entry="plugin:KeychainSecretsPlugin",
        capabilities={"read": True, "write": True, "delete": True, "keychain": True},
        modes_allowed=["research", "backtest", "paper", "live"],
    )


def test_keychain_non_macos_raises_config_error(
    monkeypatch: Any, fake_manifest: PluginManifest, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    cfg = KeychainSecretsConfig(service="algodesk_test", index_file=str(tmp_path / "idx.json"))
    ctx = build_context(Mode.RESEARCH)
    with pytest.raises(ConfigError) as exc_info:
        KeychainSecretsPlugin(fake_manifest, cfg, ctx)
    assert "Keychain secrets provider is not supported on platform" in exc_info.value.what


def test_keychain_get_set_delete_names_mocked(
    monkeypatch: Any, fake_manifest: PluginManifest, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    index_file = tmp_path / ".algodesk" / "keychain_secrets.json"
    cfg = KeychainSecretsConfig(service="algodesk_test", index_file=str(index_file))
    ctx = build_context(Mode.RESEARCH)
    provider = KeychainSecretsPlugin(fake_manifest, cfg, ctx)

    storage: dict[str, str] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # Formats:
        # find: ["security", "find-generic-password", "-s", svc, "-a", name, "-w"]
        # add: ["security", "add-generic-password", "-s", svc, "-a", name, "-w", val, "-U"]
        # delete: ["security", "delete-generic-password", "-s", svc, "-a", name]
        assert cmd[0] == "security"
        action = cmd[1]
        if action == "add-generic-password":
            name = cmd[5]
            val = cmd[7]
            storage[name] = val
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        elif action == "find-generic-password":
            name = cmd[5]
            if name in storage:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{storage[name]}\n", stderr="")
            return subprocess.CompletedProcess(
                cmd, 44, stdout="", stderr="The specified item could not be found in the keychain."
            )
        elif action == "delete-generic-password":
            name = cmd[5]
            if name in storage:
                del storage[name]
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 44, stdout="", stderr="Item not found")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Unknown action")

    monkeypatch.setattr("subprocess.run", fake_run)

    # Initially empty
    assert list(provider.names()) == []

    # Set secret
    provider.set("api_key", "secret-value-123")  # fake-secret
    assert "api_key" in provider.names()
    assert index_file.is_file()

    # Get secret
    retrieved = provider.get("api_key")
    assert retrieved == "secret-value-123"

    # Get non-existent secret raises ConfigError
    with pytest.raises(ConfigError) as exc_info:
        provider.get("missing_key")
    assert "Secret 'missing_key' not found" in exc_info.value.what

    # Delete secret
    provider.delete("api_key")
    assert "api_key" not in provider.names()
    assert list(provider.names()) == []


def test_keychain_conformance(
    fake_manifest: PluginManifest, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    cfg = KeychainSecretsConfig(service="algodesk_test", index_file=str(tmp_path / "idx.json"))
    ctx = build_context(Mode.RESEARCH)
    provider = KeychainSecretsPlugin(fake_manifest, cfg, ctx)
    problems = secrets_problems(provider)
    assert problems == []


def test_keychain_corrupted_index_file(
    monkeypatch: Any, fake_manifest: PluginManifest, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    index_file = tmp_path / "corrupt_idx.json"
    index_file.write_text("{bad json")
    cfg = KeychainSecretsConfig(service="algodesk_test", index_file=str(index_file))
    ctx = build_context(Mode.RESEARCH)
    provider = KeychainSecretsPlugin(fake_manifest, cfg, ctx)
    assert list(provider.names()) == []


def test_keychain_command_failure_raises(
    monkeypatch: Any, fake_manifest: PluginManifest, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    cfg = KeychainSecretsConfig(service="algodesk_test", index_file=str(tmp_path / "idx.json"))
    ctx = build_context(Mode.RESEARCH)
    provider = KeychainSecretsPlugin(fake_manifest, cfg, ctx)

    def failing_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["security"], 1, stdout="", stderr="Fatal permission error"
        )

    monkeypatch.setattr("subprocess.run", failing_run)
    with pytest.raises(ConfigError) as exc_info:
        provider.set("key", "val")  # fake-secret
    assert "Failed to set secret" in exc_info.value.what
