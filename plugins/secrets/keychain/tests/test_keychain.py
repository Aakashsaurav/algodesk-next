"""Tests for KeychainSecretsPlugin with fake security executable and unit tests."""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from algodesk.api import ConfigError, PluginManifest, secrets_problems

from ..config_schema import KeychainSecretsConfig
from ..plugin import KeychainSecretsPlugin


@pytest.fixture
def manifest() -> PluginManifest:
    return PluginManifest(
        name="keychain",
        kind="secrets",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:KeychainSecretsPlugin",
        capabilities={"keychain": True, "read": True, "write": True, "delete": True},
    )


@pytest.fixture
def fake_security_bin(tmp_path: Path) -> Path:
    """Create a fake `security` command line script backed by a JSON store."""
    store_file = tmp_path / "mock_keychain.json"
    store_file.write_text("{}", encoding="utf-8")

    script = tmp_path / "security"
    script_content = f"""#!/usr/bin/env python3
import sys, json
from pathlib import Path

store_path = Path({repr(str(store_file))})
data = json.loads(store_path.read_text(encoding="utf-8")) if store_path.exists() else {{}}

args = sys.argv[1:]
if not args:
    sys.exit(1)

subcmd = args[0]
if subcmd == "find-generic-password":
    account = None
    service = None
    for i in range(len(args)):
        if args[i] == "-a" and i + 1 < len(args):
            account = args[i + 1]
        elif args[i] == "-s" and i + 1 < len(args):
            service = args[i + 1]
    key = f"{{service}}:{{account}}"
    if key in data:
        print(data[key])
        sys.exit(0)
    else:
        sys.stderr.write("The specified item could not be found in the keychain.\\n")
        sys.exit(44)

elif subcmd == "add-generic-password":
    account = None
    service = None
    password = None
    for i in range(len(args)):
        if args[i] == "-a" and i + 1 < len(args):
            account = args[i + 1]
        elif args[i] == "-s" and i + 1 < len(args):
            service = args[i + 1]
        elif args[i] == "-w" and i + 1 < len(args):
            password = args[i + 1]
    if account and password is not None:
        key = f"{{service}}:{{account}}"
        data[key] = password
        store_path.write_text(json.dumps(data), encoding="utf-8")
        sys.exit(0)
    sys.exit(1)

elif subcmd == "delete-generic-password":
    account = None
    service = None
    for i in range(len(args)):
        if args[i] == "-a" and i + 1 < len(args):
            account = args[i + 1]
        elif args[i] == "-s" and i + 1 < len(args):
            service = args[i + 1]
    key = f"{{service}}:{{account}}"
    if key in data:
        del data[key]
        store_path.write_text(json.dumps(data), encoding="utf-8")
        sys.exit(0)
    sys.exit(44)

sys.exit(1)
"""
    script.write_text(script_content, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_keychain_get_set_delete(
    manifest: PluginManifest, fake_security_bin: Path, tmp_path: Path
) -> None:
    idx_file = tmp_path / "secrets_idx.json"
    cfg = KeychainSecretsConfig(service="testservice", index_file=str(idx_file))
    plugin = KeychainSecretsPlugin(
        manifest,
        cfg,
        _security_bin=str(fake_security_bin),
        _enforce_platform=False,
    )

    # Set secret
    plugin.set("api_key", "secret-value-123")  # fake-secret
    assert "api_key" in plugin.names()

    # Get secret
    val = plugin.get("api_key")
    assert val == "secret-value-123"

    # Set another
    plugin.set("token", "token-val-456")  # fake-secret
    names = plugin.names()
    assert sorted(names) == ["api_key", "token"]

    # Delete secret
    plugin.delete("api_key")
    assert "api_key" not in plugin.names()
    assert "token" in plugin.names()

    # Get deleted raises ConfigError
    with pytest.raises(ConfigError, match="not found in macOS Keychain"):
        plugin.get("api_key")


def test_keychain_missing_secret_raises_config_error(
    manifest: PluginManifest, fake_security_bin: Path, tmp_path: Path
) -> None:
    idx_file = tmp_path / "secrets_idx.json"
    cfg = KeychainSecretsConfig(service="testservice", index_file=str(idx_file))
    plugin = KeychainSecretsPlugin(
        manifest,
        cfg,
        _security_bin=str(fake_security_bin),
        _enforce_platform=False,
    )
    with pytest.raises(ConfigError) as exc_info:
        plugin.get("nonexistent")
    assert "nonexistent" in exc_info.value.what
    assert "algodesk secrets set nonexistent" in exc_info.value.fix


def test_keychain_conformance(
    manifest: PluginManifest, fake_security_bin: Path, tmp_path: Path
) -> None:
    idx_file = tmp_path / "secrets_idx.json"
    cfg = KeychainSecretsConfig(service="testservice", index_file=str(idx_file))
    plugin = KeychainSecretsPlugin(
        manifest,
        cfg,
        _security_bin=str(fake_security_bin),
        _enforce_platform=False,
    )
    problems = secrets_problems(plugin)
    assert problems == []


def test_keychain_non_darwin_raises(manifest: PluginManifest, monkeypatch: Any) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ConfigError, match="not supported on platform 'linux'"):
        KeychainSecretsPlugin(manifest, _enforce_platform=True)
