"""Plugin scaffolding utility to generate valid plugin skeletons."""

from __future__ import annotations

from pathlib import Path

from algodesk.api.errors import PluginError
from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import NAME_PATTERN


def _write_scaffold_files(target_dir: Path, k: str, name: str, class_name: str) -> None:
    tests_dir = target_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

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

    stubs = ""
    extra_imports = ""
    if k == "notifier":
        stubs = "    def send(self, message: Any) -> None:\n        pass\n"
        extra_imports = "from typing import Any\n"
    elif k == "broker":
        stubs = (
            "    def instruments(self) -> Any:\n        return []\n\n"
            "    def history(self, key: Any, start: Any, end: Any) -> Any:\n        return []\n\n"
            "    async def stream(self, instruments: Any) -> Any:\n        yield None\n"
        )
        extra_imports = "from typing import Any\n"
    elif k == "venue":
        stubs = (
            "    async def place(self, spec: Any, client_ref: str) -> Any:\n        return None\n\n"
            "    async def cancel(self, client_ref: str) -> Any:\n        return None\n\n"
            "    def on_quote(self, quote: Any) -> None:\n        pass\n\n"
            "    async def events(self) -> Any:\n        yield None\n\n"
            "    def positions(self) -> Any:\n        return []\n\n"
            "    def funds(self) -> Any:\n        return None\n"
        )
        extra_imports = "from typing import Any\n"
    elif k == "calendar":
        stubs = (
            "    def is_trading_day(self, d: Any) -> bool:\n"
            "        raise CalendarCoverageError('No data', 'scaffold', 'configure data')\n\n"
            "    def sessions(self, d: Any) -> Any:\n        return []\n\n"
            "    def next_session(self, ts: Any) -> Any:\n        return None\n"
        )
        extra_imports = (
            "from typing import Any\nfrom algodesk.api.errors import CalendarCoverageError\n"
        )
    elif k == "secrets":
        stubs = (
            "    def get(self, name: str) -> str:\n"
            "        raise ConfigError(f'Secret {name} not found', 'secrets', 'set secret')\n\n"
            "    def set(self, name: str, value: str) -> None:\n        pass\n\n"
            "    def names(self) -> list[str]:\n        return []\n"
        )
        extra_imports = "from algodesk.api.errors import ConfigError\n"
    elif k == "indicator":
        stubs = (
            f"    spec = IndicatorSpec(name='{name}')\n\n"
            "    def compute(self, values: Any) -> Any:\n        return list(values)\n\n"
            "    def new_state(self) -> Any:\n        return {}\n\n"
            "    def update(self, state: Any, value: float) -> Any:\n        return value\n"
        )
        extra_imports = "from typing import Any\nfrom algodesk.api.protocols import IndicatorSpec\n"
    elif k == "strategy":
        stubs = (
            "    params_schema = dict\n\n"
            "    def on_start(self, ctx: Any) -> None:\n        pass\n\n"
            "    def on_bar(self, ctx: Any, bar: Any) -> Any:\n        return []\n\n"
            "    def on_tick(self, ctx: Any, tick: Any) -> Any:\n        return []\n\n"
            "    def on_fill(self, ctx: Any, fill: Any) -> Any:\n        return []\n\n"
            "    def on_timer(self, ctx: Any, name: str) -> Any:\n        return []\n\n"
            "    def on_stop(self, ctx: Any) -> None:\n        pass\n\n"
            "    def state(self) -> Any:\n        return {}\n"
        )
        extra_imports = "from typing import Any\n"
    elif k == "risk_rule":
        stubs = "    def check(self, intent: Any, view: Any) -> Any:\n        return None\n"
        extra_imports = "from typing import Any\n"

    plugin_code = (
        f'"""{name} {k} plugin implementation."""\n\n'
        f"from __future__ import annotations\n\n"
        f"{extra_imports}"
        f"from algodesk.api.plugin import PluginBase\n\n\n"
        f"class {class_name}(PluginBase):\n"
        f'    """Entry class for {name} plugin."""\n\n'
        f"{stubs or '    pass\n'}"
    )
    (target_dir / "plugin.py").write_text(plugin_code, encoding="utf-8")

    test_code = (
        f'"""Tests for {name} plugin."""\n\n'
        f"def test_{name}_placeholder() -> None:\n"
        f"    assert True\n"
    )
    (tests_dir / f"test_{name}.py").write_text(test_code, encoding="utf-8")


def scaffold_plugin(
    dest_dir: Path | str,
    kind: PluginKind | str,
    name: str,
) -> Path:
    """Create a new valid plugin skeleton in dest_dir/kind/name.

    Refuses to overwrite if destination exists and is non-empty.
    """
    try:
        p_kind = kind if isinstance(kind, PluginKind) else PluginKind(kind)
    except ValueError as exc:
        allowed = ", ".join(k.value for k in PluginKind)
        raise PluginError(
            what=f"Invalid plugin kind '{kind}'. Allowed kinds: {allowed}",
            where="scaffold_plugin",
            fix=f"Specify one of the valid plugin kinds: {allowed}",
        ) from exc
    k = p_kind.value

    if not NAME_PATTERN.match(name):
        raise PluginError(
            what=f"Invalid plugin name '{name}': must match ^[a-z][a-z0-9_]{{1,40}}$",
            where="scaffold_plugin",
            fix="Choose a name with lowercase letters, digits and underscores, 2-41 characters",
        )

    target_dir = Path(dest_dir).resolve() / k / name
    if target_dir.exists() and any(target_dir.iterdir()):
        raise PluginError(
            what=f"Plugin directory already exists and is not empty: '{target_dir}'",
            where=str(target_dir),
            fix="Choose a different name or remove existing directory",
        )

    class_name = "".join(part.capitalize() for part in name.split("_")) + "Plugin"
    _write_scaffold_files(target_dir, k, name, class_name)
    return target_dir
