"""Environment variable secrets provider plugin implementation."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import cast

from algodesk.api import (
    ConfigError,
    NotSupported,
    PluginBase,
    PluginContext,
    PluginManifest,
    SecretsProvider,
)

_ENV_PREFIX = "ALGODESK_SECRET_"


class EnvSecretsPlugin(PluginBase, SecretsProvider):
    """Read-only secrets provider loading secrets from environment variables."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, object] | None = None,
        ctx: PluginContext | None = None,
    ) -> None:
        super().__init__(manifest, config or {}, cast(PluginContext, ctx))

    def get(self, name: str) -> str:
        """Read a secret from ALGODESK_SECRET_<NAME>."""
        self.require("read")
        var_name = f"{_ENV_PREFIX}{name.upper()}"
        val = os.environ.get(var_name)
        if val is None:
            raise ConfigError(
                what=f"Secret '{name}' not found in environment variable '{var_name}'",
                where=f"secret:{name}",
                fix=f'Export the variable in your shell: export {var_name}="..."',
            )
        return val

    def set(self, name: str, value: str) -> None:
        """Setting secrets in the env provider is not supported (read-only)."""
        self.require("write")
        raise NotSupported(
            what="The env secrets provider is strictly read-only and does not support set()",
            where="EnvSecretsPlugin.set",
            fix=f'Export the environment variable export {_ENV_PREFIX}{name.upper()}="..."',
        )

    def delete(self, name: str) -> None:
        """Deleting secrets in the env provider is not supported (read-only)."""
        self.require("delete")
        raise NotSupported(
            what="The env secrets provider is strictly read-only and does not support delete()",
            where="EnvSecretsPlugin.delete",
            fix=f"Unset the environment variable unset {_ENV_PREFIX}{name.upper()}",
        )

    def names(self) -> Sequence[str]:
        """Return names of available secrets found in environment variables."""
        self.require("read")
        res: list[str] = []
        for k in os.environ:
            if k.startswith(_ENV_PREFIX) and len(k) > len(_ENV_PREFIX):
                res.append(k[len(_ENV_PREFIX) :].lower())
        return sorted(res)
