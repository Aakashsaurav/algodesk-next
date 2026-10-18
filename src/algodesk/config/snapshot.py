"""Effective configuration snapshot, canonical hashing, and rendering."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from algodesk.config.loader import (
    load_platform_with_provenance,
    load_run_with_provenance,
)
from algodesk.config.models import PlatformConfig, RunConfig


@dataclass(frozen=True)
class EffectiveConfig:
    """Effective configuration snapshot including platform, optional run, sources, and hash."""

    platform: PlatformConfig
    run: RunConfig | None
    sources: Mapping[str, str]
    hash: str


def _compute_canonical_hash(
    platform: PlatformConfig,
    run: RunConfig | None,
) -> str:
    payload: dict[str, Any] = {
        "platform": platform.model_dump(mode="json"),
        "run": run.model_dump(mode="json") if run is not None else None,
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _filter_overrides(
    overrides: Mapping[str, object] | None,
    exclude_keys: set[str],
) -> dict[str, object] | None:
    if overrides is None:
        return None
    return {
        k: v for k, v in overrides.items() if k.split(".")[0].strip().lower() not in exclude_keys
    }


def _filter_env(
    env: Mapping[str, str] | None,
    exclude_keys: set[str],
) -> dict[str, str] | None:
    if env is None:
        return None
    filtered: dict[str, str] = {}
    for k, v in env.items():
        if k.startswith("ALGODESK_"):
            top = k.removeprefix("ALGODESK_").split("__")[0].strip().lower()
            if top in exclude_keys:
                continue
        filtered[k] = v
    return filtered


def build_effective(
    root: Path | str,
    run_name: str | None = None,
    profile: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> EffectiveConfig:
    """Build effective configuration from platform, profile, run, env, and CLI overrides."""
    plat_fields = set(PlatformConfig.model_fields.keys())
    run_fields = set(RunConfig.model_fields.keys())

    plat_overrides = _filter_overrides(overrides, run_fields) if run_name else overrides
    plat_env = _filter_env(env, run_fields) if run_name else env
    platform, plat_sources = load_platform_with_provenance(
        root, profile=profile, env=plat_env, overrides=plat_overrides
    )
    run: RunConfig | None = None
    sources: dict[str, str] = dict(plat_sources)

    if run_name is not None:
        run_overrides = _filter_overrides(overrides, plat_fields)
        run_env = _filter_env(env, plat_fields)
        run, run_sources = load_run_with_provenance(
            root, run_name=run_name, profile=profile, env=run_env, overrides=run_overrides
        )
        sources.update(run_sources)

    eff_hash = _compute_canonical_hash(platform, run)
    return EffectiveConfig(
        platform=platform,
        run=run,
        sources=sources,
        hash=eff_hash,
    )


def _collect_leaf_pairs(data: Mapping[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    leaves: list[tuple[str, Any]] = []
    for key, val in data.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, Mapping) and val:
            leaves.extend(_collect_leaf_pairs(val, full_key))
        else:
            leaves.append((full_key, val))
    return leaves


def _format_leaf_value(val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, bool):
        return str(val).lower()
    return str(val)


def render_effective(eff: EffectiveConfig) -> str:
    """Render effective configuration as human-readable text with sources."""
    short_hash = eff.hash.removeprefix("sha256:")[:12]
    lines: list[str] = [f"EFFECTIVE CONFIG sha256:{short_hash}"]

    leaves_dict: dict[str, Any] = {}
    plat_dict = eff.platform.model_dump(mode="python", exclude_none=True)
    for k, v in _collect_leaf_pairs(plat_dict):
        leaves_dict[k] = v

    if eff.run is not None:
        run_dict = eff.run.model_dump(mode="python", exclude_none=True)
        for k, v in _collect_leaf_pairs(run_dict):
            leaves_dict[k] = v

    for leaf in sorted(leaves_dict.keys()):
        val = leaves_dict[leaf]
        formatted = _format_leaf_value(val)
        source = eff.sources.get(leaf, "default")
        lines.append(f"{leaf} = {formatted}   # {source}")

    return "\n".join(lines)
