"""TOML configuration loader with layered deep-merge, provenance, and error conversion."""

from __future__ import annotations

import copy
import os
import re
import tomllib
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from algodesk.api.errors import ConfigError
from algodesk.config.models import PlatformConfig, RunConfig

_LINE_NUM_RE = re.compile(r"\(at line (\d+)")

_SCRIPTS_ENV_EXPORTS: frozenset[str] = frozenset(
    {
        "ROOT",
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_BIN_DIR",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "UV_NO_CONFIG",
        "PRE_COMMIT_HOME",
        "PYTHONNOUSERSITE",
        "PIP_REQUIRE_VIRTUALENV",
    }
)

_SECRET_KEYWORDS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "private_key",
    "credential",
)
_SECRET_REF_RE: re.Pattern[str] = re.compile(r"^secret:([A-Za-z0-9_.-]{1,64})$")
_BASE64_LIKE_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")


def _extract_toml_line(msg: str) -> str | None:
    match = _LINE_NUM_RE.search(msg)
    return match.group(1) if match else None


def load_toml(path: Path | str) -> dict[str, Any]:
    """Load a TOML file from *path*."""
    p = Path(path)
    if not p.is_file():
        msg = f"Configuration file not found: {p}"
        raise ConfigError(
            what=msg,
            where=f"{p} -> file",
            fix=f"Create '{p}' or check that the path is correct",
        )
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        msg = str(exc)
        line_num = _extract_toml_line(msg)
        where = f"{p}:{line_num}" if line_num else str(p)
        raise ConfigError(
            what=f"TOML syntax error: {msg}",
            where=where,
            fix="Fix the syntax error at the specified line in the TOML file",
        ) from exc


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base*."""
    result: dict[str, Any] = {}
    for key, val in base.items():
        if key in override:
            override_val = override[key]
            if isinstance(val, Mapping) and isinstance(override_val, Mapping):
                result[key] = deep_merge(val, override_val)
            else:
                result[key] = copy.deepcopy(override_val)
        else:
            result[key] = copy.deepcopy(val)
    for key, val in override.items():
        if key not in base:
            result[key] = copy.deepcopy(val)
    return result


def _collect_leaves(data: Mapping[str, Any], prefix: str = "") -> list[str]:
    leaves: list[str] = []
    for key, val in data.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, Mapping) and val:
            leaves.extend(_collect_leaves(val, full_key))
        else:
            leaves.append(full_key)
    return leaves


def merge_layers(
    layers: Sequence[tuple[str, Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge a sequence of (source_path, data_dict) layers in order."""
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}

    for source, layer_dict in layers:
        for leaf in _collect_leaves(layer_dict):
            provenance[leaf] = source
            prefix = f"{leaf}."
            to_remove = [k for k in provenance if k.startswith(prefix)]
            for k in to_remove:
                del provenance[k]
            parts = leaf.split(".")
            for i in range(1, len(parts)):
                ancestor = ".".join(parts[:i])
                provenance.pop(ancestor, None)
        merged = deep_merge(merged, layer_dict)

    return merged, provenance


def _validate_safe_name(name: str, kind: str, base_dir: Path) -> None:
    if not name or "/" in name or "\\" in name or ".." in name:
        target_path = base_dir / f"{name}.toml"
        msg = f"Invalid {kind} name '{name}': names cannot contain path separators or '..'"
        raise ConfigError(
            what=msg,
            where=f"{target_path} -> {kind}.name",
            fix="Use a plain name without path separators or '..' (e.g. 'demo_paper')",
        )


def _resolve_provenance(
    dotted_key: str,
    provenance: Mapping[str, str],
    fallback: str,
) -> str:
    if dotted_key in provenance:
        return provenance[dotted_key]
    parts = dotted_key.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in provenance:
            return provenance[candidate]
        prefix = f"{candidate}."
        for k, src in provenance.items():
            if k.startswith(prefix):
                return src
        parts.pop()
    return fallback


def _get_target_model(
    model_cls: type[BaseModel],
    loc: tuple[int | str, ...],
) -> type[BaseModel] | None:
    curr: Any = model_cls
    for part in loc[:-1]:
        if isinstance(part, str) and hasattr(curr, "model_fields") and part in curr.model_fields:
            field = curr.model_fields[part]
            ann = field.annotation
            origin = typing.get_origin(ann)
            if origin is list:
                args = typing.get_args(ann)
                curr = args[0] if args else ann
            else:
                curr = ann
        elif isinstance(part, int):
            continue
    return curr if hasattr(curr, "model_fields") else None


def _format_type_error(
    err: Mapping[str, Any],
    dotted_key: str,
    root_model: type[BaseModel],
    loc: tuple[int | str, ...],
    msg: str,
) -> tuple[str, str]:
    ctx: Mapping[str, Any] = err.get("ctx", {}) or {}
    expected = ctx.get("expected")
    if not expected:
        target_model = _get_target_model(root_model, loc)
        if target_model and loc and str(loc[-1]) in target_model.model_fields:
            ann = target_model.model_fields[str(loc[-1])].annotation
            expected = getattr(ann, "__name__", str(ann))
    got = type(err.get("input")).__name__
    fix = f"Expected {expected or 'valid type'}, got {got}"
    return f"Invalid type for '{dotted_key}': {msg}", fix


def _format_value_error(dotted_key: str, msg: str) -> tuple[str, str]:
    if "Universe requires exactly one" in msg:
        fix = "Specify exactly one of 'screen' or 'symbols' under [universe]"
    elif "At least one strategy" in msg:
        fix = "Define at least one strategy entry under [[strategies]]"
    elif "allocated_capital must be greater than 0" in msg:
        fix = "Set allocated_capital to a positive amount (> 0)"
    elif "relative path without traversal" in msg:
        fix = "Use a relative path without traversal ('..')"
    else:
        fix = f"Adjust '{dotted_key}' to satisfy validation requirements"
    return f"Invalid value for '{dotted_key}': {msg}", fix


def _format_error_details(
    err: Mapping[str, Any],
    dotted_key: str,
    root_model: type[BaseModel],
) -> tuple[str, str]:
    err_type = str(err.get("type", ""))
    msg = str(err.get("msg", ""))
    ctx: Mapping[str, Any] = err.get("ctx", {}) or {}
    loc = tuple(err.get("loc", ()))

    if err_type == "extra_forbidden":
        bad_key = str(loc[-1]) if loc else dotted_key
        target_model = _get_target_model(root_model, loc)
        allowed = (
            sorted(target_model.model_fields.keys())
            if target_model and hasattr(target_model, "model_fields")
            else []
        )
        fix = f"Allowed keys: {', '.join(allowed)}" if allowed else "Remove the unknown key"
        return f"Unknown configuration key '{bad_key}'", fix

    if err_type in (
        "string_type",
        "int_parsing",
        "int_type",
        "float_type",
        "float_parsing",
        "bool_type",
        "list_type",
        "dict_type",
        "mapping_type",
        "decimal_parsing",
    ):
        return _format_type_error(err, dotted_key, root_model, loc, msg)

    if err_type == "enum":
        expected = ctx.get("expected")
        fix = f"Allowed values: {expected}" if expected else "Choose a valid enum value"
        return f"Invalid value for '{dotted_key}': {msg}", fix

    if err_type in ("greater_than", "greater_than_equal", "less_than", "less_than_equal"):
        bound = ctx.get("gt", ctx.get("ge", 0))
        fix = f"Value must be greater than {bound}"
        return f"Invalid value for '{dotted_key}': {msg}", fix

    if err_type == "value_error":
        return _format_value_error(dotted_key, msg)

    if err_type == "missing":
        return f"Missing required configuration key '{dotted_key}'", f"Provide '{dotted_key}'"

    return f"Configuration error at '{dotted_key}': {msg}", "Check configuration schema"


def _validation_error_to_config_error(
    exc: ValidationError,
    provenance: Mapping[str, str],
    layers: Sequence[tuple[str, Mapping[str, Any]]],
    root_model: type[BaseModel],
) -> ConfigError:
    first_err = exc.errors()[0]
    loc = tuple(first_err.get("loc", ()))
    dotted_key = ".".join(str(p) for p in loc) if loc else "root"
    fallback_file = layers[-1][0] if layers else "config"
    file_path = _resolve_provenance(dotted_key, provenance, fallback_file)
    where = f"{file_path} -> {dotted_key}"
    what, fix = _format_error_details(first_err, dotted_key, root_model)
    return ConfigError(what=what, where=where, fix=fix)


def _get_reserved_env_vars(root: Path | None = None) -> set[str]:
    reserved = set(_SCRIPTS_ENV_EXPORTS)
    if root is not None:
        env_sh = root / "scripts" / "env.sh"
        if env_sh.is_file():
            try:
                for line in env_sh.read_text(encoding="utf-8").splitlines():
                    trimmed = line.strip()
                    if trimmed.startswith("export "):
                        var = trimmed.removeprefix("export ").split("=")[0].strip()
                        if var:
                            reserved.add(var)
            except OSError:
                pass
    return reserved


def _parse_env_scalar_or_array(raw: str) -> Any:
    try:
        data = tomllib.loads(f"v = {raw}")
        return data["v"]
    except Exception:
        return raw


def _build_nested_layer(segments: Sequence[str], value: Any) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    curr = nested
    for seg in segments[:-1]:
        curr[seg] = {}
        curr = curr[seg]
    curr[segments[-1]] = value
    return nested


def _extract_env_layers(
    env: Mapping[str, str],
    root: Path | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    reserved = _get_reserved_env_vars(root)
    layers: list[tuple[str, dict[str, Any]]] = []
    for var_name, raw_val in sorted(env.items()):
        if var_name in reserved or var_name.removeprefix("ALGODESK_") in reserved:
            continue
        if not var_name.startswith("ALGODESK_"):
            continue
        suffix = var_name.removeprefix("ALGODESK_")
        if not suffix:
            continue
        segments = [s.lower() for s in suffix.split("__") if s]
        if not segments:
            continue
        val = _parse_env_scalar_or_array(raw_val)
        layer_dict = _build_nested_layer(segments, val)
        layers.append((f"env:{var_name}", layer_dict))
    return layers


def _extract_cli_layers(
    overrides: Mapping[str, object] | None,
) -> list[tuple[str, dict[str, Any]]]:
    if not overrides:
        return []
    layers: list[tuple[str, dict[str, Any]]] = []
    for dotted_key, val in sorted(overrides.items()):
        segments = [s.strip().lower() for s in dotted_key.split(".") if s.strip()]
        if not segments:
            continue
        layer_dict = _build_nested_layer(segments, val)
        layers.append((f"cli:{dotted_key}", layer_dict))
    return layers


def _check_secret_string(
    val: str,
    is_secret_key: bool,
    dotted_key: str,
    source: str,
) -> None:
    if is_secret_key:
        if not _SECRET_REF_RE.match(val):
            raise ConfigError(
                what="plain secret in config",
                where=f"{source} -> {dotted_key}",
                fix="store it with your secrets provider and write secret:<name>",
            )
    else:
        if _SECRET_REF_RE.match(val):
            return
        if _BASE64_LIKE_RE.match(val):
            raise ConfigError(
                what="plain secret in config",
                where=f"{source} -> {dotted_key}",
                fix="store it with your secrets provider and write secret:<name>",
            )


def _validate_secrets_recursive(
    data: Any,
    provenance: Mapping[str, str],
    prefix: str,
    fallback_source: str,
) -> None:
    if isinstance(data, Mapping):
        for key, val in data.items():
            child_key = f"{prefix}.{key}" if prefix else str(key)
            last_segment = str(key).lower()
            is_secret = any(kw in last_segment for kw in _SECRET_KEYWORDS)
            source = _resolve_provenance(child_key, provenance, fallback_source)
            if isinstance(val, str):
                _check_secret_string(val, is_secret, child_key, source)
            elif isinstance(val, (list, tuple)):
                for idx, item in enumerate(val):
                    item_key = f"{child_key}[{idx}]"
                    if isinstance(item, str):
                        _check_secret_string(item, is_secret, child_key, source)
                    elif isinstance(item, (Mapping, list, tuple)):
                        _validate_secrets_recursive(item, provenance, item_key, fallback_source)
            elif isinstance(val, Mapping):
                _validate_secrets_recursive(val, provenance, child_key, fallback_source)


def validate_secrets(
    data: Mapping[str, Any],
    provenance: Mapping[str, str],
    fallback_source: str = "config",
) -> None:
    """Validate that secret values use references and no plain secrets exist."""
    _validate_secrets_recursive(data, provenance, "", fallback_source)


@dataclass(frozen=True)
class ResolvedPaths:
    """Absolute workspace paths for platform runtime artifacts and plugins."""

    data: Path
    logs: Path
    results: Path
    local: Path
    plugin_dirs: list[Path]


def _check_and_resolve_path(
    root_resolved: Path,
    path_str: str,
    field_name: str,
) -> Path:
    raw_path = Path(path_str)
    if ".." in raw_path.parts:
        msg = f"Path '{path_str}' contains traversal '..' escaping workspace root (NFR-14)"
        raise ConfigError(
            what=msg,
            where=f"paths -> {field_name}",
            fix=f"Use a relative path inside '{root_resolved}' without '..' traversal",
        )
    target = raw_path if raw_path.is_absolute() else (root_resolved / raw_path)
    resolved = target.resolve()
    if not resolved.is_relative_to(root_resolved) or resolved == root_resolved:
        msg = f"Path '{path_str}' resolves outside workspace root (NFR-14)"
        raise ConfigError(
            what=msg,
            where=f"paths -> {field_name}",
            fix=f"Ensure path resolves inside workspace root '{root_resolved}'",
        )
    return resolved


def resolve_paths(root: Path | str, cfg: PlatformConfig) -> ResolvedPaths:
    """Resolve and validate platform paths inside the workspace root (NFR-14)."""
    root_resolved = Path(root).resolve()
    data = _check_and_resolve_path(root_resolved, cfg.paths.data_dir, "data_dir")
    logs = _check_and_resolve_path(root_resolved, cfg.paths.logs_dir, "logs_dir")
    results = _check_and_resolve_path(root_resolved, cfg.paths.results_dir, "results_dir")
    local = _check_and_resolve_path(root_resolved, cfg.paths.local_dir, "local_dir")
    plugin_dirs: list[Path] = [
        _check_and_resolve_path(root_resolved, p, f"plugin_dirs[{i}]")
        for i, p in enumerate(cfg.plugin_dirs)
    ]
    return ResolvedPaths(
        data=data,
        logs=logs,
        results=results,
        local=local,
        plugin_dirs=plugin_dirs,
    )


def ensure_dirs(paths: ResolvedPaths) -> None:
    """Ensure data, logs, results, and local directories exist with mode 0o700."""
    for d in (paths.data, paths.logs, paths.results, paths.local):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        d.chmod(0o700)


def load_platform_with_provenance(
    root: Path | str,
    profile: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> tuple[PlatformConfig, dict[str, str]]:
    """Load platform configuration and return model and provenance mapping."""
    root_path = Path(root)
    layers: list[tuple[str, Mapping[str, Any]]] = []

    platform_file = root_path / "config" / "platform.toml"
    if platform_file.is_file():
        layers.append((str(platform_file), load_toml(platform_file)))

    if profile is not None:
        profiles_dir = root_path / "config" / "profiles"
        _validate_safe_name(profile, "profile", profiles_dir)
        profile_file = profiles_dir / f"{profile}.toml"
        if not profile_file.is_file():
            msg = f"Profile configuration file not found: {profile_file}"
            raise ConfigError(
                what=msg,
                where=f"{profile_file} -> profile",
                fix=f"Create '{profile_file}' or choose an existing profile",
            )
        layers.append((str(profile_file), load_toml(profile_file)))

    effective_env = os.environ if env is None else env
    layers.extend(_extract_env_layers(effective_env, root_path))
    layers.extend(_extract_cli_layers(overrides))

    merged, provenance = merge_layers(layers)
    fallback = layers[-1][0] if layers else "config"
    validate_secrets(merged, provenance, fallback_source=fallback)
    try:
        return PlatformConfig.model_validate(merged), provenance
    except ValidationError as exc:
        raise _validation_error_to_config_error(exc, provenance, layers, PlatformConfig) from exc


def load_platform(
    root: Path | str,
    profile: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> PlatformConfig:
    """Load platform configuration from config/platform.toml, profile, env, and CLI overrides."""
    cfg, _ = load_platform_with_provenance(root, profile=profile, env=env, overrides=overrides)
    return cfg


def load_run_with_provenance(
    root: Path | str,
    run_name: str,
    profile: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> tuple[RunConfig, dict[str, str]]:
    """Load run configuration and return model and provenance mapping."""
    root_path = Path(root)
    layers: list[tuple[str, Mapping[str, Any]]] = []

    platform_file = root_path / "config" / "platform.toml"
    if platform_file.is_file():
        plat_data = load_toml(platform_file)
        plat_filtered = {
            k: v
            for k, v in plat_data.items()
            if k
            not in (set(PlatformConfig.model_fields.keys()) - set(RunConfig.model_fields.keys()))
        }
        if plat_filtered:
            layers.append((str(platform_file), plat_filtered))

    if profile is not None:
        profiles_dir = root_path / "config" / "profiles"
        _validate_safe_name(profile, "profile", profiles_dir)
        profile_file = profiles_dir / f"{profile}.toml"
        if not profile_file.is_file():
            msg = f"Profile configuration file not found: {profile_file}"
            raise ConfigError(
                what=msg,
                where=f"{profile_file} -> profile",
                fix=f"Create '{profile_file}' or choose an existing profile",
            )
        layers.append((str(profile_file), load_toml(profile_file)))

    runs_dir = root_path / "config" / "runs"
    _validate_safe_name(run_name, "run", runs_dir)
    run_file = runs_dir / f"{run_name}.toml"
    if not run_file.is_file():
        msg = f"Run configuration file not found: {run_file}"
        raise ConfigError(
            what=msg,
            where=f"{run_file} -> run",
            fix=f"Create '{run_file}' or specify an existing run name",
        )
    layers.append((str(run_file), load_toml(run_file)))

    effective_env = os.environ if env is None else env
    layers.extend(_extract_env_layers(effective_env, root_path))
    layers.extend(_extract_cli_layers(overrides))

    merged, provenance = merge_layers(layers)
    fallback = layers[-1][0] if layers else "config"
    validate_secrets(merged, provenance, fallback_source=fallback)
    try:
        return RunConfig.model_validate(merged), provenance
    except ValidationError as exc:
        raise _validation_error_to_config_error(exc, provenance, layers, RunConfig) from exc


def load_run(
    root: Path | str,
    run_name: str,
    profile: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> RunConfig:
    """Load run configuration from platform, profile, run, env, and CLI overrides."""
    cfg, _ = load_run_with_provenance(
        root, run_name=run_name, profile=profile, env=env, overrides=overrides
    )
    return cfg
