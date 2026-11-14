"""Tests ensuring screen conformance rejects forbidden imports and malformed outputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from algodesk.api.conformance import screen_problems
from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.plugin import PluginBase
from algodesk.api.screens import FeatureView, ScreenSpec


def _make_manifest(name: str) -> PluginManifest:
    return PluginManifest(
        name=name,
        kind=PluginKind.SCREEN,
        version="0.1.0",
        api_version=1,
        entry=f"test:{name}",
        capabilities={"default": True},
    )


def test_conformance_flags_forbidden_data_import(tmp_path: Path) -> None:
    # Write a dummy plugin module with forbidden import
    mod_file = tmp_path / "bad_data_plugin.py"
    mod_file.write_text(
        "import algodesk.data.service\n"
        "from algodesk.api.plugin import PluginBase\n"
        "from algodesk.api.screens import ScreenSpec\n\n"
        "class BadDataPlugin(PluginBase):\n"
        "    spec = ScreenSpec(name='bad_data')\n"
        "    def evaluate(self, f):\n"
        "        return []\n",
        encoding="utf-8",
    )
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("bad_data_mod", mod_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bad_data_mod"] = mod
    spec.loader.exec_module(mod)
    cls = mod.BadDataPlugin
    plugin = cls(manifest=_make_manifest("bad_data"), config={}, ctx=None)

    problems = screen_problems(plugin)
    assert any("algodesk.data" in p for p in problems)


def test_conformance_flags_forbidden_oms_import(tmp_path: Path) -> None:
    mod_file = tmp_path / "bad_oms_plugin.py"
    mod_file.write_text(
        "from algodesk.oms import manager\n"
        "from algodesk.api.plugin import PluginBase\n"
        "from algodesk.api.screens import ScreenSpec\n\n"
        "class BadOmsPlugin(PluginBase):\n"
        "    spec = ScreenSpec(name='bad_oms')\n"
        "    def evaluate(self, f):\n"
        "        return []\n",
        encoding="utf-8",
    )
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("bad_oms_mod", mod_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bad_oms_mod"] = mod
    spec.loader.exec_module(mod)
    cls = mod.BadOmsPlugin
    plugin = cls(manifest=_make_manifest("bad_oms"), config={}, ctx=None)

    problems = screen_problems(plugin)
    assert any("algodesk.oms" in p for p in problems)


def test_conformance_flags_row_missing_score() -> None:
    class MissingScoreRow:
        def __init__(self, symbol: str, passed: bool) -> None:
            self.symbol = symbol
            self.passed = passed
            self.values = {}

    class BadRowPlugin(PluginBase):
        spec = ScreenSpec(name="bad_row_spec")

        def __init__(self, manifest: Any = None, config: Any = None, ctx: Any = None) -> None:
            super().__init__(manifest, config or {}, ctx)

        def evaluate(self, f: FeatureView) -> list[Any]:
            return [MissingScoreRow("SYM1", True)]

    plugin = BadRowPlugin(manifest=_make_manifest("bad_row"), config={}, ctx=None)
    problems = screen_problems(plugin)
    assert any("score" in p for p in problems)


def test_conformance_flags_row_missing_values() -> None:
    class MissingValuesRow:
        def __init__(self, symbol: str, passed: bool, score: float) -> None:
            self.symbol = symbol
            self.passed = passed
            self.score = score

    class BadValuesPlugin(PluginBase):
        spec = ScreenSpec(name="bad_val_spec")

        def __init__(self, manifest: Any = None, config: Any = None, ctx: Any = None) -> None:
            super().__init__(manifest, config or {}, ctx)

        def evaluate(self, f: FeatureView) -> list[Any]:
            return [MissingValuesRow("SYM1", True, 100.0)]

    plugin = BadValuesPlugin(manifest=_make_manifest("bad_val"), config={}, ctx=None)
    problems = screen_problems(plugin)
    assert any("values" in p for p in problems)


def test_conformance_flags_evaluate_raising_exception() -> None:
    class ExplodingPlugin(PluginBase):
        spec = ScreenSpec(name="exploding")

        def __init__(self, manifest: Any = None, config: Any = None, ctx: Any = None) -> None:
            super().__init__(manifest, config or {}, ctx)

        def evaluate(self, f: FeatureView) -> list[Any]:
            raise RuntimeError("Computation crashed")

    plugin = ExplodingPlugin(manifest=_make_manifest("exploding"), config={}, ctx=None)
    problems = screen_problems(plugin)
    assert any("Computation crashed" in p for p in problems)
