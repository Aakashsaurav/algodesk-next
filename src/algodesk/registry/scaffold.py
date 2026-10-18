"""Plugin scaffolding utility to generate valid plugin skeletons."""

from __future__ import annotations

from pathlib import Path

from algodesk.api.errors import PluginError
from algodesk.api.kinds import PluginKind


def scaffold_plugin(
    dest_dir: Path | str,
    kind: PluginKind | str,
    name: str,
) -> Path:
    """Create a new valid plugin skeleton in dest_dir/kind/name.

    Refuses to overwrite if destination exists and is non-empty.
    """
    k = kind.value if isinstance(kind, PluginKind) else str(kind)
    target_dir = Path(dest_dir).resolve() / k / name

    if target_dir.exists() and any(target_dir.iterdir()):
        raise PluginError(
            what=f"Plugin directory already exists and is not empty: '{target_dir}'",
            where=str(target_dir),
            fix="Choose a different name or remove existing directory",
        )

    tests_dir = target_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    class_name = "".join(part.capitalize() for part in name.split("_")) + "Plugin"

    manifest_content = (
        f'name = "{name}"\n'
        f'kind = "{k}"\n'
        f'version = "0.1.0"\n'
        f"api_version = 1\n"
        f'entry = "plugin:{class_name}"\n\n'
        f"[capabilities]\n"
        f"default = true\n"
    )
    (target_dir / "plugin.toml").write_text(manifest_content, encoding="utf-8")

    readme_content = f"# {name}\n\n## Summary\n{name} {k} plugin for AlgoDesk Next.\n"
    (target_dir / "README.md").write_text(readme_content, encoding="utf-8")

    plugin_code = (
        f'"""{name} {k} plugin implementation."""\n\n'
        f"from __future__ import annotations\n\n"
        f"from algodesk.api.plugin import PluginBase\n\n\n"
        f"class {class_name}(PluginBase):\n"
        f'    """Entry class for {name} plugin."""\n'
    )
    (target_dir / "plugin.py").write_text(plugin_code, encoding="utf-8")

    test_code = (
        f'"""Tests for {name} plugin."""\n\n'
        f"def test_{name}_placeholder() -> None:\n"
        f"    assert True\n"
    )
    (tests_dir / f"test_{name}.py").write_text(test_code, encoding="utf-8")

    return target_dir
