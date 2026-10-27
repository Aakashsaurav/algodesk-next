"""Instrument groups loader from config/groups/*.toml.

Enables grouping instruments by thematic collections (e.g. demo5, banks, indices)
for universe selection, data querying, and bulk deletion.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from algodesk.api.errors import ConfigError


def _resolve_groups_dir(groups_dir: Path | str | None) -> Path:
    if groups_dir is not None:
        p = Path(groups_dir)
        return p if p.is_absolute() else Path.cwd() / p
    return Path.cwd() / "config" / "groups"


def load_groups(groups_dir: Path | str | None = None) -> dict[str, list[str]]:
    """Load all instrument groups returning a mapping of name to member list."""
    p = _resolve_groups_dir(groups_dir)
    if not p.is_dir():
        return {}

    groups: dict[str, list[str]] = {}
    for toml_path in sorted(p.glob("*.toml")):
        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
            name = str(data.get("name", toml_path.stem))
            members_raw = data.get("members", [])
            members = [str(m).strip() for m in members_raw if str(m).strip()]
            groups[name] = members
        except Exception as exc:
            msg = f"Failed to load group TOML file: {exc}"
            raise ConfigError(
                what=msg, where=str(toml_path), fix="Verify valid TOML syntax"
            ) from exc

    return groups


def list_group_details(groups_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Load full group metadata including name, description, and members."""
    p = _resolve_groups_dir(groups_dir)
    if not p.is_dir():
        return {}

    details: dict[str, dict[str, Any]] = {}
    for toml_path in sorted(p.glob("*.toml")):
        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
            name = str(data.get("name", toml_path.stem))
            desc = str(data.get("description", ""))
            members_raw = data.get("members", [])
            members = [str(m).strip() for m in members_raw if str(m).strip()]
            details[name] = {
                "name": name,
                "description": desc,
                "members": members,
                "file": toml_path.name,
            }
        except Exception as exc:
            msg = f"Failed to load group details: {exc}"
            raise ConfigError(
                what=msg, where=str(toml_path), fix="Verify valid TOML syntax"
            ) from exc

    return details


def get_group(name: str, groups_dir: Path | str | None = None) -> list[str] | None:
    """Retrieve members for a single group by name, or None if not found."""
    groups = load_groups(groups_dir)
    return groups.get(name)
