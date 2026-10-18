"""TOML configuration loader with layered deep-merge, provenance, and error conversion."""

from __future__ import annotations

import copy
import re
import tomllib
import typing
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from algodesk.api.errors import ConfigError
from algodesk.config.models import PlatformConfig, RunConfig

_LINE_NUM_RE = re.compile(r"\(at line (\d+)")


def _extract_toml_line(msg: str) -> str | None:
    match = _LINE_NUM_RE.search(msg)
    return match.group(1) if match else None


def load_toml(path: Path | str) -> dict[str, Any]:
    """Load a TOML file from *path*.

    Raises
    ------
    ConfigError
        If the file is missing or contains TOML syntax errors.
    """
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
    """Recursively merge *override* into *base*.

    Dicts merge recursively; lists and scalars are replaced.
    Neither input mapping is mutated.
    """
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
        # pyrefly: ignore[unnecessary-type-conversion]
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, Mapping) and val:
            leaves.extend(_collect_leaves(val, full_key))
        else:
            leaves.append(full_key)
    return leaves


def merge_layers(
    layers: Sequence[tuple[str, Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge a sequence of (source_path, data_dict) layers in order.

    Returns
    -------
    merged : dict
        Deeply-merged configuration dictionary.
    provenance : dict
        Mapping from dotted leaf key to the source path that set or overrode it.
    """
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}

    for source, layer_dict in layers:
        for leaf in _collect_leaves(layer_dict):
            provenance[leaf] = source
            # Remove any previous children if a dict became a scalar/list
            prefix = f"{leaf}."
            to_remove = [k for k in provenance if k.startswith(prefix)]
            for k in to_remove:
                del provenance[k]
            # Remove any ancestor if a scalar became a dict
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
        expected = ctx.get("expected")
        if not expected:
            target_model = _get_target_model(root_model, loc)
            if target_model and loc and str(loc[-1]) in target_model.model_fields:
                ann = target_model.model_fields[str(loc[-1])].annotation
                expected = getattr(ann, "__name__", str(ann))
        got = type(err.get("input")).__name__
        fix = f"Expected {expected or 'valid type'}, got {got}"
        return f"Invalid type for '{dotted_key}': {msg}", fix

    if err_type == "enum":
        expected = ctx.get("expected")
        fix = f"Allowed values: {expected}" if expected else "Choose a valid enum value"
        return f"Invalid value for '{dotted_key}': {msg}", fix

    if err_type in ("greater_than", "greater_than_equal", "less_than", "less_than_equal"):
        bound = ctx.get("gt", ctx.get("ge", 0))
        fix = f"Value must be greater than {bound}"
        return f"Invalid value for '{dotted_key}': {msg}", fix

    if err_type == "value_error":
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


def load_platform(root: Path | str, profile: str | None = None) -> PlatformConfig:
    """Load platform configuration from config/platform.toml and optional profile."""
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

    merged, provenance = merge_layers(layers)
    try:
        return PlatformConfig.model_validate(merged)
    except ValidationError as exc:
        raise _validation_error_to_config_error(exc, provenance, layers, PlatformConfig) from exc


def load_run(root: Path | str, run_name: str, profile: str | None = None) -> RunConfig:
    """Load run configuration from platform, profile, and run layers."""
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

    merged, provenance = merge_layers(layers)
    try:
        return RunConfig.model_validate(merged)
    except ValidationError as exc:
        raise _validation_error_to_config_error(exc, provenance, layers, RunConfig) from exc
