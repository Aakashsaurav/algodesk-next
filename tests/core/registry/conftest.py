"""Shared fixtures and plugin creation helpers for registry tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest


class DummyParamReader:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = values or {}

    def get(self, name: str, scope: str | None = None) -> Any:
        return self.values.get(name, "dummy_val")


class DummyJournalWriter:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object], bool]] = []

    def append(
        self,
        kind: str,
        payload: Mapping[str, object],
        *,
        durable: bool = False,
    ) -> int:
        self.events.append((kind, payload, durable))
        return len(self.events)


class DummySecretReader:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = secrets or {}

    def get(self, name: str) -> str:
        return self.secrets.get(name, "dummy_secret")


def make_context(mode: Any) -> Any:
    from algodesk.api.services import PluginContext

    return PluginContext(
        mode=mode,
        run_id="test-run-123",
        params=DummyParamReader(),
        journal=DummyJournalWriter(),
        secrets=DummySecretReader(),
    )


def create_fake_plugin(
    root: Path,
    kind: str = "notifier",
    name: str = "fake_plugin",
    *,
    version: str = "0.1.0",
    entry: str = "plugin:FakePlugin",
    capabilities: dict[str, bool] | None = None,
    modes_allowed: Sequence[str] | None = None,
    order_capable: bool = False,
    config_schema: str | None = None,
    extra_toml: str = "",
    readme_text: str | None = None,
    create_tests: bool = True,
    plugin_py_content: str | None = None,
    extra_files: dict[str, str] | None = None,
) -> Path:
    plugin_dir = root / kind / name
    plugin_dir.mkdir(parents=True, exist_ok=True)

    caps = capabilities or {"send": True}
    caps_lines = "\n".join(f"{k} = {str(v).lower()}" for k, v in caps.items())

    modes = modes_allowed or (["live"] if order_capable else ["research", "backtest", "paper"])
    modes_str = ", ".join(f'"{m}"' for m in modes)

    schema_line = f'config_schema = "{config_schema}"\n' if config_schema else ""

    manifest = (
        f'name = "{name}"\n'
        f'kind = "{kind}"\n'
        f'version = "{version}"\n'
        f"api_version = 1\n"
        f'entry = "{entry}"\n'
        f"modes_allowed = [{modes_str}]\n"
        f"order_capable = {str(order_capable).lower()}\n"
        f"{schema_line}"
        f"{extra_toml}\n"
        f"[capabilities]\n"
        f"{caps_lines}\n"
    )
    (plugin_dir / "plugin.toml").write_text(manifest, encoding="utf-8")

    if readme_text is None:
        readme_text = f"# {name}\n\n## Summary\nSummary of {name} plugin.\n"
    if readme_text != "__OMIT__":
        (plugin_dir / "README.md").write_text(readme_text, encoding="utf-8")

    if create_tests:
        tests_dir = plugin_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_fake.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    mod_name, cls_name = entry.split(":", 1)
    if plugin_py_content is None:
        plugin_py_content = (
            "from __future__ import annotations\n"
            "from algodesk.api.plugin import PluginBase\n\n"
            f"class {cls_name}(PluginBase):\n"
            "    pass\n"
        )
    if plugin_py_content != "__OMIT__":
        mod_file = plugin_dir / f"{mod_name}.py"
        mod_file.write_text(plugin_py_content, encoding="utf-8")

    if extra_files:
        for fname, content in extra_files.items():
            fpath = plugin_dir / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")

    return plugin_dir


@pytest.fixture
def fake_plugin_factory() -> Callable[..., Path]:
    return create_fake_plugin


@pytest.fixture
def context_factory() -> Callable[..., Any]:
    return make_context
