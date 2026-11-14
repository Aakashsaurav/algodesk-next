"""Tests for sma_trend_py screen plugin."""

import tomllib
from pathlib import Path

from algodesk.api.conformance import screen_problems
from algodesk.api.manifest import parse_manifest

from ..plugin import SmaTrendPyPlugin


def test_sma_trend_py_conformance() -> None:
    p_toml = Path(__file__).parent.parent / "plugin.toml"
    with open(p_toml, "rb") as f:
        data = tomllib.load(f)
    manifest = parse_manifest(data, str(p_toml))
    plugin = SmaTrendPyPlugin(manifest=manifest)
    problems = screen_problems(plugin)
    assert not problems, f"Conformance problems: {problems}"
