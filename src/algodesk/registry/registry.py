"""Plugin registry: discovery, conformance gating and safe lazy loading."""

from __future__ import annotations

import builtins
import importlib.util
import sys
import types
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from algodesk.api.errors import ConfigError, PluginError, SafetyViolation
from algodesk.api.kinds import Mode, PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.plugin import Plugin
from algodesk.api.services import PluginContext
from algodesk.registry.conformance import ConformanceStore
from algodesk.registry.discovery import Issue, PluginInfo, discover


class PluginRegistry:
    """Manages plugin discovery, conformance checks, and safe loading."""

    def __init__(
        self,
        root: Path | str,
        plugin_dirs: Sequence[Path | str] | Path | str,
        *,
        state_dir: Path | str,
    ) -> None:
        self.root = Path(root).resolve()
        raw_dirs: Sequence[Path | str]
        if isinstance(plugin_dirs, (str, Path)):
            raw_dirs = [plugin_dirs]
        else:
            raw_dirs = plugin_dirs
        self._plugin_dirs: builtins.list[Path] = [
            Path(p).resolve() if Path(p).is_absolute() else (self.root / p).resolve()
            for p in raw_dirs
        ]
        s_dir = Path(state_dir)
        self.state_dir = s_dir.resolve() if s_dir.is_absolute() else (self.root / s_dir).resolve()
        self._store = ConformanceStore(self.state_dir)
        self._plugins: dict[tuple[str, str], PluginInfo] = {}
        self._issues: builtins.list[Issue] = []
        self._loaded: builtins.list[tuple[PluginKind, str, Mode]] = []
        self.refresh()

    def refresh(self) -> None:
        """Scan plugin directories and update discovered plugins and issues."""
        plugins, issues = discover(self._plugin_dirs)
        self._plugins = {(p.kind.value, p.name): p for p in plugins}
        self._issues = issues

    @property
    def issues(self) -> builtins.list[Issue]:
        """Return all issues detected during discovery."""
        return builtins.list(self._issues)

    def list(self, kind: PluginKind | str | None = None) -> builtins.list[PluginInfo]:
        """List discovered plugins, optionally filtered by kind."""
        if kind is None:
            return builtins.list(self._plugins.values())
        k = kind.value if isinstance(kind, PluginKind) else str(kind)
        return [p for p in self._plugins.values() if p.kind.value == k]

    def info(self, kind: PluginKind | str, name: str) -> PluginInfo:
        """Return PluginInfo for a plugin or raise PluginError."""
        k = kind.value if isinstance(kind, PluginKind) else str(kind)
        if (k, name) not in self._plugins:
            raise PluginError(
                what=f"Plugin '{k}/{name}' not found",
                where=f"{k}/{name}",
                fix="Verify plugin exists and passes discovery",
            )
        return self._plugins[(k, name)]

    def record_conformance(
        self,
        kind: PluginKind | str,
        name: str,
        suite: str,
    ) -> None:
        """Record conformance test pass using current code hash."""
        p_info = self.info(kind, name)
        self._store.record_pass(p_info.kind, p_info.name, suite, p_info.code_hash)

    def conformance_current(self, kind: PluginKind | str, name: str) -> bool:
        """Check if plugin conformance pass record matches current code hash."""
        p_info = self.info(kind, name)
        return self._store.is_current(p_info.kind, p_info.name, p_info.code_hash)

    def conformance_status(self, kind: PluginKind | str, name: str) -> str:
        """Return conformance status: 'current', 'stale', or 'none'."""
        p_info = self.info(kind, name)
        return self._store.status(p_info.kind, p_info.name, p_info.code_hash)

    def loaded(self) -> builtins.list[tuple[PluginKind, str, Mode]]:
        """Return list of (kind, name, mode) for plugins loaded in this process."""
        return builtins.list(self._loaded)

    def _check_mode_and_safety(
        self,
        manifest: PluginManifest,
        ctx: PluginContext,
        k: str,
        name: str,
    ) -> None:
        if manifest.order_capable and ctx.mode != Mode.LIVE:
            raise SafetyViolation(
                what=(
                    f"Order-capable plugin '{k}/{name}' cannot be loaded in mode "
                    f"'{ctx.mode.value}'; order-capable plugins are only permitted in 'live' mode"
                ),
                where=f"{k}/{name}",
                fix="Run in 'live' mode or disable order_capable on the plugin",
            )

        if ctx.mode not in manifest.modes_allowed:
            allowed = ", ".join(str(m.value) for m in manifest.modes_allowed)
            raise PluginError(
                what=(
                    f"Plugin '{k}/{name}' cannot run in mode '{ctx.mode.value}' "
                    f"(allowed: {allowed})"
                ),
                where=f"{k}/{name}",
                fix=f"Run in an allowed mode or add '{ctx.mode.value}' to modes_allowed",
            )

        if ctx.mode in (Mode.PAPER, Mode.LIVE):
            if not self.conformance_current(k, name):
                raise PluginError(
                    what=(
                        f"Plugin '{k}/{name}' requires a passing conformance record before loading "
                        f"in mode '{ctx.mode.value}'"
                    ),
                    where=f"{k}/{name}",
                    fix=f"algodesk plugins test {k} {name}",
                )

    def _validate_config(
        self,
        info: PluginInfo,
        config: Mapping[str, object],
        k: str,
        name: str,
    ) -> None:
        manifest = info.manifest
        if not manifest.config_schema:
            if config:
                opts = builtins.list(config.keys())
                raise PluginError(
                    what=(
                        f"Plugin '{k}/{name}' declares no config_schema but received "
                        f"config options: {opts}"
                    ),
                    where=f"{k}/{name}",
                    fix="Remove config options or declare a config_schema in plugin.toml",
                )
            return

        schema_val = manifest.config_schema
        schema_file = (
            info.directory / schema_val
            if schema_val.endswith(".py") or (info.directory / schema_val).is_file()
            else info.directory / f"{schema_val.split(':', 1)[0]}.py"
        )
        model_name = schema_val.split(":", 1)[1] if ":" in schema_val else "ConfigSchema"

        mod_name = f"_algodesk_schema_{k}_{name}"
        spec = importlib.util.spec_from_file_location(mod_name, str(schema_file))
        if spec is None or spec.loader is None:
            raise PluginError(
                what=f"Could not load schema module '{schema_file}'",
                where=f"{k}/{name}",
                fix=f"Ensure schema file '{schema_file.name}' is valid",
            )
        schema_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(schema_mod)

        model_cls = getattr(schema_mod, model_name, None)
        if model_cls is None:
            raise PluginError(
                what=f"Schema model '{model_name}' not found in {schema_file.name}",
                where=f"{k}/{name}",
                fix=f"Define class '{model_name}' in {schema_file.name}",
            )

        try:
            model_cls.model_validate(config)
        except Exception as exc:
            err_msg = str(exc)
            if hasattr(exc, "errors"):
                try:
                    paths = [".".join(str(p) for p in err["loc"]) for err in exc.errors()]
                    err_msg = f"validation error for key(s) {paths}"
                except Exception:
                    pass
            raise ConfigError(
                what=f"Configuration failed validation for plugin '{k}/{name}': {err_msg}",
                where=f"{k}/{name}",
                fix=f"Ensure configuration options match schema in {schema_file.name}",
            ) from exc

    def _import_entry_module(
        self,
        info: PluginInfo,
        k: str,
        name: str,
    ) -> tuple[types.ModuleType, str, str, Path]:
        mod_part, cls_name = info.manifest.entry.split(":", 1)
        pkg_name = f"_algodesk_plugins_{k}_{name}"
        if pkg_name not in sys.modules:
            pkg = types.ModuleType(pkg_name)
            pkg.__path__ = [str(info.directory)]
            pkg.__file__ = str(info.directory / "__init__.py")
            sys.modules[pkg_name] = pkg

        mod_full_name = f"{pkg_name}.{mod_part}"
        plugin_file = info.directory / f"{mod_part}.py"
        if not plugin_file.is_file():
            plugin_file = info.directory / f"{mod_part.split('.')[-1]}.py"

        spec = importlib.util.spec_from_file_location(
            mod_full_name,
            str(plugin_file),
        )
        if spec is None or spec.loader is None:
            raise PluginError(
                what=f"Could not load module spec for '{info.manifest.entry}'",
                where=f"{k}/{name}",
                fix=f"Ensure {plugin_file.name} exists and is valid Python",
            )
        plugin_mod = importlib.util.module_from_spec(spec)
        plugin_mod.__package__ = pkg_name
        sys.modules[mod_full_name] = plugin_mod
        try:
            spec.loader.exec_module(plugin_mod)
        except Exception as exc:
            sys.modules.pop(mod_full_name, None)
            raise PluginError(
                what=f"Failed to import plugin entry module: {exc}",
                where=f"{k}/{name}",
                fix="Resolve import errors in plugin module",
            ) from exc
        return plugin_mod, cls_name, mod_part, plugin_file

    def _load_and_instantiate(
        self,
        info: PluginInfo,
        config: Mapping[str, object],
        ctx: PluginContext,
        k: str,
        name: str,
    ) -> Plugin:
        plugin_mod, cls_name, mod_part, plugin_file = self._import_entry_module(info, k, name)
        cls = getattr(plugin_mod, cls_name, None)
        if cls is None:
            raise PluginError(
                what=(
                    f"Entry class '{cls_name}' not found in module '{mod_part}' "
                    f"({plugin_file.name})"
                ),
                where=f"{k}/{name}",
                fix=f"Define class '{cls_name}' in {plugin_file.name} (module '{mod_part}')",
            )

        try:
            instance = cls(info.manifest, config, ctx)
        except Exception as exc:
            raise PluginError(
                what=f"Failed to instantiate plugin '{cls_name}': {type(exc).__name__}: {exc}",
                where=f"{k}/{name}",
                fix="Ensure constructor accepts (manifest, config, ctx) and succeeds",
            ) from exc

        if not isinstance(instance, Plugin):
            raise PluginError(
                what=f"Plugin instance '{cls_name}' does not implement Plugin protocol",
                where=f"{k}/{name}",
                fix="Ensure entry class inherits from PluginBase or implements Plugin protocol",
            )

        if instance.manifest != info.manifest:
            raise PluginError(
                what=f"Plugin instance manifest mismatch for '{k}/{name}'",
                where=f"{k}/{name}",
                fix="Ensure plugin retains original manifest without mutation",
            )

        self._loaded.append((info.kind, info.name, ctx.mode))
        return instance

    def create(
        self,
        kind: PluginKind | str,
        name: str,
        *,
        config: Mapping[str, object] | None = None,
        ctx: PluginContext,
    ) -> Plugin:
        """Create and instantiate a plugin in the given context."""
        k = kind.value if isinstance(kind, PluginKind) else str(kind)
        info = self.info(k, name)
        cfg = config or {}
        self._check_mode_and_safety(info.manifest, ctx, k, name)
        self._validate_config(info, cfg, k, name)
        return self._load_and_instantiate(info, cfg, ctx, k, name)


def get_run_config_plugins(run_config: Any) -> builtins.list[tuple[PluginKind, str]]:
    """Return all (PluginKind, name) pairs required by the run configuration."""
    plugins: builtins.list[tuple[PluginKind, str]] = []
    if getattr(run_config, "data_source", None) is not None:
        p = getattr(run_config.data_source, "plugin", None)
        if p:
            plugins.append((PluginKind.BROKER, str(p)))
    if getattr(run_config, "execution_venue", None) is not None:
        p = getattr(run_config.execution_venue, "plugin", None)
        if p:
            plugins.append((PluginKind.VENUE, str(p)))
    if getattr(run_config, "fees", None) is not None:
        p = getattr(run_config.fees, "schedule", None)
        if p:
            plugins.append((PluginKind.FEES, str(p)))
    if getattr(run_config, "secrets", None) is not None:
        p = getattr(run_config.secrets, "plugin", None)
        if p:
            plugins.append((PluginKind.SECRETS, str(p)))
    for c in getattr(run_config, "calendars", []) or []:
        p = getattr(c, "plugin", None)
        if p:
            plugins.append((PluginKind.CALENDAR, str(p)))
    for s in getattr(run_config, "strategies", []) or []:
        p = getattr(s, "plugin", None)
        if p:
            plugins.append((PluginKind.STRATEGY, str(p)))
    for n in getattr(run_config, "notifiers", []) or []:
        p = getattr(n, "plugin", None)
        if p:
            plugins.append((PluginKind.NOTIFIER, str(p)))
    plugins.append((PluginKind.RISK_RULE, "builtin"))
    plugins.append((PluginKind.INDICATOR, "builtin"))
    return plugins
