"""macOS Keychain secrets provider plugin implementation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from algodesk.api import (
    ConfigError,
    PluginBase,
    PluginContext,
    PluginManifest,
    SecretsProvider,
)

from .config_schema import KeychainSecretsConfig


class KeychainSecretsPlugin(PluginBase, SecretsProvider):
    """Secure secrets provider wrapping the macOS Keychain CLI tool."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: KeychainSecretsConfig | Mapping[str, Any] | None = None,
        ctx: PluginContext | None = None,
        *,
        _security_bin: str = "security",
        _enforce_platform: bool = True,
    ) -> None:
        if isinstance(config, KeychainSecretsConfig):
            self._cfg = config
            raw_config: Mapping[str, object] = config.model_dump()
        elif isinstance(config, Mapping):
            self._cfg = KeychainSecretsConfig.model_validate(config)
            raw_config = config
        else:
            self._cfg = KeychainSecretsConfig()
            raw_config = self._cfg.model_dump()

        super().__init__(manifest, raw_config, cast(PluginContext, ctx))
        self._service = self._cfg.service
        self._security_bin = _security_bin
        self._enforce_platform = _enforce_platform
        self._index_file = self._resolve_index_file()

        if self._enforce_platform and sys.platform != "darwin":
            raise ConfigError(
                what=f"Keychain secrets provider is not supported on platform '{sys.platform}'",
                where="KeychainSecretsPlugin.__init__",
                fix="Use the 'env' secrets provider or run AlgoDesk on macOS",
            )

    def _resolve_index_file(self) -> Path:
        if self._cfg.index_file:
            p = Path(self._cfg.index_file).resolve()
            p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            return p

        # Look for .algodesk in current directory or user home
        cwd_state = Path.cwd() / ".algodesk"
        if cwd_state.is_dir() or cwd_state.parent.exists():
            cwd_state.mkdir(mode=0o700, parents=True, exist_ok=True)
            return cwd_state / "keychain_secrets.json"

        home_state = Path.home() / ".algodesk"
        home_state.mkdir(mode=0o700, parents=True, exist_ok=True)
        return home_state / "keychain_secrets.json"

    def _load_index(self) -> list[str]:
        if not self._index_file.is_file():
            return []
        try:
            data = json.loads(self._index_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(k) for k in data if isinstance(k, str)]
            if isinstance(data, dict):
                names = data.get("names", [])
                return [str(k) for k in names if isinstance(k, str)]
        except Exception:
            return []
        return []

    def _save_index(self, names: Sequence[str]) -> None:
        self._index_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        unique_sorted = sorted(set(names))
        tmp = self._index_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"names": unique_sorted}, indent=2), encoding="utf-8")
        os.replace(tmp, self._index_file)

    def get(self, name: str) -> str:
        """Retrieve a secret value from the macOS Keychain by account name."""
        self.require("read")
        cmd = [self._security_bin, "find-generic-password", "-s", self._service, "-a", name, "-w"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise ConfigError(
                what=f"Keychain security tool '{self._security_bin}' not found on system PATH",
                where=f"secret:{name}",
                fix="Ensure macOS standard command line utilities are installed in /usr/bin",
            ) from exc

        if res.returncode != 0:
            raise ConfigError(
                what=f"Secret '{name}' not found in macOS Keychain under service '{self._service}'",
                where=f"secret:{name}",
                fix=f"Store the secret using 'algodesk secrets set {name}'",
            )

        return res.stdout.rstrip("\r\n")

    def set(self, name: str, value: str) -> None:
        """Store a secret value securely in the macOS Keychain and index its name."""
        self.require("write")
        cmd = [
            self._security_bin,
            "add-generic-password",
            "-s",
            self._service,
            "-a",
            name,
            "-w",
            value,
            "-U",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise ConfigError(
                what=f"Keychain security tool '{self._security_bin}' not found on system PATH",
                where=f"secret:{name}",
                fix="Ensure macOS standard command line utilities are installed in /usr/bin",
            ) from exc

        if res.returncode != 0:
            err = res.stderr.strip() or f"exit code {res.returncode}"
            raise ConfigError(
                what=f"Failed to set secret '{name}' in macOS Keychain: {err}",
                where=f"secret:{name}",
                fix="Check Keychain access permissions and unlock the keychain",
            )

        current = self._load_index()
        if name not in current:
            current.append(name)
            self._save_index(current)

    def delete(self, name: str) -> None:
        """Delete a secret from the macOS Keychain and remove it from the index."""
        self.require("delete")
        cmd = [self._security_bin, "delete-generic-password", "-s", self._service, "-a", name]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise ConfigError(
                what=f"Keychain security tool '{self._security_bin}' not found on system PATH",
                where=f"secret:{name}",
                fix="Ensure macOS standard command line utilities are installed in /usr/bin",
            ) from exc

        current = self._load_index()
        if name in current:
            current = [k for k in current if k != name]
            self._save_index(current)

        if res.returncode != 0:
            # Check if secret didn't exist
            pass

    def names(self) -> Sequence[str]:
        """List registered secret names (names only, values are never returned)."""
        self.require("read")
        return self._load_index()
