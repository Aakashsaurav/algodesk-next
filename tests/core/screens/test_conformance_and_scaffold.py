"""Tests for screen plugin conformance suite and plugin scaffolding."""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from algodesk.api.conformance import kind_problems, screen_problems
from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import PluginManifest, parse_manifest
from algodesk.api.plugin import PluginBase
from algodesk.api.screens import FeatureView, ScreenRow, ScreenSpec
from algodesk.registry.scaffold import scaffold_plugin


class ConformingScreenPlugin(PluginBase):
    spec = ScreenSpec(
        name="conforming_screen",
        description="A conforming screen plugin",
        timeframe="1d",
        output_columns=("val",),
    )

    def __init__(
        self,
        manifest: Any = None,
        config: Mapping[str, object] | None = None,
        ctx: Any = None,
    ) -> None:
        super().__init__(manifest, config or {}, ctx)

    def evaluate(self, features: FeatureView) -> Sequence[ScreenRow]:
        return [
            ScreenRow(symbol=s, passed=True, score=100.0, values={"val": 1.0})
            for s in features.symbols
        ]


def _make_manifest(name: str, entry: str) -> PluginManifest:
    return PluginManifest(
        name=name,
        kind=PluginKind.SCREEN,
        version="0.1.0",
        api_version=1,
        entry=entry,
        capabilities={"default": True},
    )


def test_conforming_screen_passes() -> None:
    manifest = _make_manifest("conforming_screen", "test:ConformingScreenPlugin")
    plugin = ConformingScreenPlugin(manifest=manifest)
    problems = screen_problems(plugin)
    assert not problems


def test_kind_problems_dispatches_to_screen() -> None:
    manifest = _make_manifest("conforming_screen", "test:ConformingScreenPlugin")
    plugin = ConformingScreenPlugin(manifest=manifest)
    assert kind_problems(plugin) == []


def test_conformance_fails_when_missing_spec() -> None:
    class MissingSpecPlugin(PluginBase):
        def __init__(self, manifest: Any = None, config: Any = None, ctx: Any = None) -> None:
            super().__init__(manifest, config or {}, ctx)

        def evaluate(self, features: FeatureView) -> Sequence[ScreenRow]:
            return []

    manifest = _make_manifest("missing_spec", "test:MissingSpecPlugin")
    plugin = MissingSpecPlugin(manifest=manifest)
    problems = screen_problems(plugin)
    assert any("spec" in p for p in problems)


def test_conformance_fails_when_missing_evaluate() -> None:
    class MissingEvalPlugin(PluginBase):
        spec = ScreenSpec(name="missing_eval")

        def __init__(self, manifest: Any = None, config: Any = None, ctx: Any = None) -> None:
            super().__init__(manifest, config or {}, ctx)

    manifest = _make_manifest("missing_eval", "test:MissingEvalPlugin")
    plugin = MissingEvalPlugin(manifest=manifest)
    problems = screen_problems(plugin)
    assert any("evaluate" in p for p in problems)


def test_conformance_fails_when_rows_invalid_type() -> None:
    class BadRowsPlugin(PluginBase):
        spec = ScreenSpec(name="bad_rows")

        def __init__(self, manifest: Any = None, config: Any = None, ctx: Any = None) -> None:
            super().__init__(manifest, config or {}, ctx)

        def evaluate(self, features: FeatureView) -> Any:
            return "not a sequence"

    manifest = _make_manifest("bad_rows", "test:BadRowsPlugin")
    plugin = BadRowsPlugin(manifest=manifest)
    problems = screen_problems(plugin)
    assert any("Sequence" in p for p in problems)


def test_conformance_fails_when_non_deterministic() -> None:
    class NonDeterministicPlugin(PluginBase):
        spec = ScreenSpec(name="non_det")
        _counter = 0

        def __init__(self, manifest: Any = None, config: Any = None, ctx: Any = None) -> None:
            super().__init__(manifest, config or {}, ctx)

        def evaluate(self, features: FeatureView) -> Sequence[ScreenRow]:
            self._counter += 1
            return [
                ScreenRow(
                    symbol=s,
                    passed=True,
                    score=float(self._counter * 10),
                    values={},
                )
                for s in features.symbols
            ]

    manifest = _make_manifest("non_det", "test:NonDeterministicPlugin")
    plugin = NonDeterministicPlugin(manifest=manifest)
    problems = screen_problems(plugin)
    assert any("deterministic" in p.lower() for p in problems)


def test_scaffold_screen_plugin(tmp_path: Path) -> None:
    target_dir = scaffold_plugin(tmp_path, "screen", "alpha_screen")
    assert target_dir.is_dir()
    assert (target_dir / "plugin.toml").is_file()
    assert (target_dir / "plugin.py").is_file()
    assert (target_dir / "README.md").is_file()
    assert (target_dir / "tests" / "test_alpha_screen.py").is_file()

    import tomllib

    p_toml = target_dir / "plugin.toml"
    with open(p_toml, "rb") as f:
        data = tomllib.load(f)
    manifest = parse_manifest(data, str(p_toml))
    assert manifest.name == "alpha_screen"
    assert manifest.kind == PluginKind.SCREEN

    # Dynamically load scaffolded plugin and assert conformance
    spec = importlib.util.spec_from_file_location("plugin", target_dir / "plugin.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls = mod.AlphaScreenPlugin
    plugin = cls(manifest=manifest)

    problems = screen_problems(plugin)
    assert not problems, f"Scaffolded screen failed conformance: {problems}"
