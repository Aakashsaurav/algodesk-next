"""Runtime lifecycle, resource management, and composition root."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from algodesk.api.kinds import Mode
from algodesk.config.loader import ResolvedPaths, ensure_dirs, resolve_paths
from algodesk.config.snapshot import EffectiveConfig, build_effective
from algodesk.ops.paths import StatePaths, find_project_root
from algodesk.params.registry import ParamRegistry
from algodesk.registry.registry import PluginRegistry


@dataclass(frozen=True)
class Runtime:
    """Encapsulates active AlgoDesk runtime configuration, paths, and registries."""

    root: Path
    effective: EffectiveConfig
    paths: ResolvedPaths
    state: StatePaths
    params: ParamRegistry | None
    plugins: PluginRegistry

    def close(self) -> None:
        """Release all runtime resources."""
        if self.params is not None:
            self.params.close()

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _init_param_registry(
    root: Path,
    state: StatePaths,
    mode: Mode,
    *,
    ensure: bool,
) -> ParamRegistry | None:
    db_exists = False
    try:
        db_exists = state.params_db.exists()
    except OSError:
        db_exists = False

    if not (db_exists or ensure):
        return None

    if ensure:
        state.params_db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    reg = ParamRegistry(state.params_db, mode=mode)
    defaults_dir = root / "config" / "params"
    if defaults_dir.is_dir():
        reg.load_defaults(defaults_dir)
    return reg


def open_runtime(
    root: Path | str | None = None,
    *,
    run: str | Path | None = None,
    profile: str | None = None,
    mode: Mode = Mode.RESEARCH,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
    ensure: bool = False,
) -> Runtime:
    """Open AlgoDesk runtime, resolving configuration, paths, and component registries."""
    root_path = Path(root).resolve() if root is not None else find_project_root()
    run_name = (
        Path(run).stem if run and Path(run).suffix == ".toml" else (str(run) if run else None)
    )

    effective = build_effective(
        root_path, run_name=run_name, profile=profile, env=env, overrides=overrides
    )
    paths = resolve_paths(root_path, effective.platform)
    if ensure:
        ensure_dirs(paths)

    state = StatePaths.resolve(root_path, paths.data)
    effective_mode = effective.run.run.mode if effective.run is not None else mode
    params = _init_param_registry(root_path, state, effective_mode, ensure=ensure)

    if ensure:
        state.conformance_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    plugins = PluginRegistry(root_path, paths.plugin_dirs, state_dir=state.conformance_dir)
    return Runtime(
        root=root_path,
        effective=effective,
        paths=paths,
        state=state,
        params=params,
        plugins=plugins,
    )
