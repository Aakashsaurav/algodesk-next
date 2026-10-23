"""Plugin discovery, manifest inspection and issue collection."""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import PluginManifest, parse_manifest
from algodesk.registry.conformance import code_hash


@dataclass(frozen=True)
class Issue:
    """An issue detected during plugin discovery."""

    path: Path
    what: str
    fix: str

    def __str__(self) -> str:
        return f"{self.what} | {self.path} | {self.fix}"


@dataclass(frozen=True)
class PluginInfo:
    """Discovered metadata for a valid plugin."""

    kind: PluginKind
    name: str
    manifest: PluginManifest
    directory: Path
    code_hash: str


def _check_readme(plugin_dir: Path) -> list[Issue]:
    readme_file = plugin_dir / "README.md"
    if not readme_file.exists():
        return [
            Issue(
                path=readme_file,
                what="Missing README.md",
                fix="Create README.md with a Summary section",
            )
        ]
    try:
        text = readme_file.read_text(encoding="utf-8")
    except Exception as exc:
        return [
            Issue(
                path=readme_file,
                what=f"Could not read README.md: {exc}",
                fix="Ensure README.md is valid UTF-8 text",
            )
        ]
    if not text.strip():
        return [
            Issue(
                path=readme_file,
                what="README.md is empty",
                fix="Add content and a Summary heading to README.md",
            )
        ]

    lines = text.splitlines()[:40]
    has_summary = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            heading_text = stripped.lstrip("#").strip().lower()
            if heading_text.startswith("summary") or "summary" in heading_text:
                has_summary = True
                break

    if not has_summary:
        return [
            Issue(
                path=readme_file,
                what="README.md missing a 'Summary' heading in its first 40 lines",
                fix="Add a '# Summary' or '## Summary' section in the first 40 lines",
            )
        ]
    return []


def _check_entry_and_schema(
    plugin_dir: Path,
    manifest: PluginManifest | None,
    raw_data: dict[str, Any] | None,
) -> list[Issue]:
    issues: list[Issue] = []
    # Entry module check
    entry_val: Any = None
    if manifest is not None:
        entry_val = manifest.entry
    elif isinstance(raw_data, dict):
        entry_val = raw_data.get("entry")

    if isinstance(entry_val, str) and ":" in entry_val:
        mod_part = entry_val.split(":", 1)[0]
        candidates = [plugin_dir / f"{mod_part}.py"]
        if "." in mod_part:
            candidates.append(plugin_dir / Path(*mod_part.split(".")).with_suffix(".py"))
            candidates.append(plugin_dir / f"{mod_part.split('.')[-1]}.py")
        if not any(c.is_file() for c in candidates):
            target = candidates[0] if "." not in mod_part else candidates[-1]
            issues.append(
                Issue(
                    path=target,
                    what=f"Entry module file missing for entry '{entry_val}'",
                    fix=f"Create {target.name} in {plugin_dir}",
                )
            )

    # Config schema check
    schema_val: Any = None
    if manifest is not None:
        schema_val = manifest.config_schema
    elif isinstance(raw_data, dict):
        schema_val = raw_data.get("config_schema")

    if isinstance(schema_val, str) and schema_val:
        if schema_val.endswith(".py"):
            schema_path = plugin_dir / schema_val
        elif ":" in schema_val:
            mod_part = schema_val.split(":", 1)[0]
            schema_path = plugin_dir / f"{mod_part}.py"
        elif (plugin_dir / schema_val).is_file():
            schema_path = plugin_dir / schema_val
        else:
            schema_path = plugin_dir / f"{schema_val}.py"

        if not schema_path.is_file():
            issues.append(
                Issue(
                    path=schema_path,
                    what=f"Config schema file missing: '{schema_val}'",
                    fix=f"Create {schema_path.name} in {plugin_dir}",
                )
            )
    return issues


def _normalize_kind(folder: str) -> str:
    mapping = {
        "brokers": "broker",
        "venues": "venue",
        "fees": "fees",
        "indicators": "indicator",
        "screens": "screen",
        "strategies": "strategy",
        "risk_rules": "risk_rule",
        "notifiers": "notifier",
        "calendars": "calendar",
        "secrets": "secrets",
        "transports": "transport",
        "models": "model",
        "reports": "report",
    }
    return mapping.get(folder, folder)


def _check_plugin_dir(
    plugin_dir: Path,
    kind_folder: str,
    name_folder: str,
    seen_plugins: dict[tuple[str, str], Path],
) -> tuple[PluginInfo | None, list[Issue]]:
    issues: list[Issue] = []
    plugin_toml = plugin_dir / "plugin.toml"

    raw_data: dict[str, Any] | None = None
    if not plugin_toml.exists():
        issues.append(
            Issue(
                path=plugin_toml,
                what="Missing plugin.toml",
                fix="Create a valid plugin.toml manifest in the plugin directory",
            )
        )
    else:
        try:
            raw_data = tomllib.loads(plugin_toml.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append(
                Issue(
                    path=plugin_toml,
                    what=f"Bad TOML: {exc}",
                    fix="Fix TOML syntax errors in plugin.toml",
                )
            )

    manifest: PluginManifest | None = None
    if raw_data is not None:
        try:
            manifest = parse_manifest(raw_data, str(plugin_toml))
        except Exception as exc:
            what = getattr(exc, "what", str(exc))
            fix = getattr(exc, "fix", "Fix manifest fields in plugin.toml")
            issues.append(
                Issue(
                    path=plugin_toml,
                    what=f"Invalid manifest: {what}",
                    fix=fix,
                )
            )

    # Name and kind checks
    name_val = manifest.name if manifest else (raw_data.get("name") if raw_data else None)
    kind_val = (
        manifest.kind.value
        if manifest
        else (raw_data.get("kind") if raw_data and isinstance(raw_data.get("kind"), str) else None)
    )

    if isinstance(name_val, str) and name_val != name_folder:
        issues.append(
            Issue(
                path=plugin_toml,
                what=f"Folder name '{name_folder}' does not match manifest name '{name_val}'",
                fix=f"Rename folder to '{name_val}' or change name in plugin.toml",
            )
        )

    norm_folder = _normalize_kind(kind_folder)
    if isinstance(kind_val, str) and kind_val != norm_folder:
        issues.append(
            Issue(
                path=plugin_toml,
                what=f"Folder kind '{kind_folder}' does not match manifest kind '{kind_val}'",
                fix=f"Move plugin to '{kind_val}/' or change kind in plugin.toml",
            )
        )

    if isinstance(name_val, str) and isinstance(kind_val, str):
        key = (kind_val, name_val)
        if key in seen_plugins:
            first_path = seen_plugins[key]
            issues.append(
                Issue(
                    path=plugin_dir,
                    what=(
                        f"Duplicate plugin for kind '{kind_val}' and name '{name_val}' "
                        f"(already defined at {first_path})"
                    ),
                    fix="Remove duplicate plugin or rename it",
                )
            )
        else:
            seen_plugins[key] = plugin_dir

    issues.extend(_check_readme(plugin_dir))

    tests_dir = plugin_dir / "tests"
    if not tests_dir.is_dir():
        issues.append(
            Issue(
                path=tests_dir,
                what="Missing tests/ folder",
                fix="Create a tests/ directory with test files for this plugin",
            )
        )

    issues.extend(_check_entry_and_schema(plugin_dir, manifest, raw_data))

    if not issues and manifest is not None:
        info = PluginInfo(
            kind=manifest.kind,
            name=manifest.name,
            manifest=manifest,
            directory=plugin_dir,
            code_hash=code_hash(plugin_dir),
        )
        return info, []

    return None, issues


def discover(
    plugin_dirs: Sequence[Path | str] | Path | str,
) -> tuple[list[PluginInfo], list[Issue]]:
    """Scan plugin directories for plugins, collecting all issues.

    Parameters
    ----------
    plugin_dirs : sequence of paths
        Directories to search for plugin packages under <kind>/<name>.

    Returns
    -------
    tuple[list[PluginInfo], list[Issue]]
        Valid plugins and all detected issues.
    """
    if isinstance(plugin_dirs, (str, Path)):
        dirs = [Path(plugin_dirs)]
    else:
        dirs = [Path(p) for p in plugin_dirs]

    plugins: list[PluginInfo] = []
    all_issues: list[Issue] = []
    seen_plugins: dict[tuple[str, str], Path] = {}

    for base_dir in dirs:
        resolved_base = base_dir.resolve()
        if not resolved_base.is_dir():
            continue
        for kind_entry in sorted(resolved_base.iterdir()):
            if not kind_entry.is_dir() or kind_entry.name.startswith("_"):
                continue
            kind_folder = kind_entry.name
            for plugin_entry in sorted(kind_entry.iterdir()):
                if not plugin_entry.is_dir() or plugin_entry.name.startswith("_"):
                    continue
                name_folder = plugin_entry.name
                info, issues = _check_plugin_dir(
                    plugin_entry,
                    kind_folder,
                    name_folder,
                    seen_plugins,
                )
                if info is not None:
                    plugins.append(info)
                all_issues.extend(issues)

    return plugins, all_issues
