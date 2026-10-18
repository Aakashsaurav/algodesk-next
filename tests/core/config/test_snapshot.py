"""Tests for EffectiveConfig, build_effective, canonical hashing, and render_effective."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

_RUN_TOML = """
allocated_capital = 500000
[run]
name = "demo"
mode = "paper"
[data_source]
plugin = "upstox"
[execution_venue]
plugin = "sim"
[calendar]
plugin = "nse"
[secrets]
plugin = "keychain"
[fees]
schedule = "zerodha_nse"
[deployment]
profile = "local"
[universe]
screen = "nifty_50"
[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
"""


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_effective_config_is_frozen(tmp_path: Path) -> None:
    """EffectiveConfig is a frozen dataclass."""
    from algodesk.config.snapshot import build_effective

    _write_file(tmp_path / "config" / "platform.toml", 'project = "algodesk"\n')
    eff = build_effective(tmp_path)
    with pytest.raises(FrozenInstanceError):
        eff.hash = "new_hash"  # type: ignore[misc]


def test_build_effective_platform_only(tmp_path: Path) -> None:
    """build_effective loads platform-only config when run_name is None."""
    from algodesk.config.snapshot import build_effective

    _write_file(
        tmp_path / "config" / "platform.toml",
        'project = "test_workbench"\n[logging]\nlevel = "DEBUG"\n',
    )
    eff = build_effective(tmp_path)
    assert eff.platform.project == "test_workbench"
    assert eff.platform.logging.level == "DEBUG"
    assert eff.run is None
    assert eff.hash.startswith("sha256:")
    assert "project" in eff.sources
    assert "logging.level" in eff.sources


def test_build_effective_with_run(tmp_path: Path) -> None:
    """build_effective loads both platform and run when run_name is provided."""
    from algodesk.config.snapshot import build_effective

    _write_file(tmp_path / "config" / "platform.toml", 'project = "algodesk"\n')
    _write_file(tmp_path / "config" / "runs" / "demo.toml", _RUN_TOML)

    eff = build_effective(tmp_path, run_name="demo")
    assert eff.platform.project == "algodesk"
    assert eff.run is not None
    assert eff.run.allocated_capital == Decimal("500000")
    assert "allocated_capital" in eff.sources
    assert "project" in eff.sources


def test_build_effective_with_env_and_cli(tmp_path: Path) -> None:
    """build_effective respects env and CLI overrides with accurate provenance."""
    from algodesk.config.snapshot import build_effective

    _write_file(tmp_path / "config" / "platform.toml", 'project = "algodesk"\n')
    env = {"ALGODESK_LOGGING__LEVEL": "DEBUG"}
    overrides = {"logging.format": "text"}

    eff = build_effective(tmp_path, env=env, overrides=overrides)
    assert eff.platform.logging.level == "DEBUG"
    assert eff.platform.logging.format == "text"
    assert eff.sources["logging.level"] == "env:ALGODESK_LOGGING__LEVEL"
    assert eff.sources["logging.format"] == "cli:logging.format"


def test_canonical_hash_stable_across_key_order(tmp_path: Path) -> None:
    """Hash is canonical JSON (sorted keys, no spaces) and stable across dict key ordering."""
    from algodesk.config.snapshot import build_effective

    _write_file(
        tmp_path / "config" / "platform.toml",
        'project = "algodesk"\nplugin_dirs = ["plugins"]\n',
    )
    eff1 = build_effective(tmp_path)
    eff2 = build_effective(tmp_path)
    assert eff1.hash == eff2.hash
    assert eff1.hash.startswith("sha256:")
    assert len(eff1.hash.removeprefix("sha256:")) == 64


def test_canonical_hash_changes_with_value(tmp_path: Path) -> None:
    """Changing any configuration value changes the canonical hash."""
    from algodesk.config.snapshot import build_effective

    _write_file(
        tmp_path / "config" / "platform.toml",
        'project = "algodesk"\n[logging]\nlevel = "INFO"\n',
    )
    eff_info = build_effective(tmp_path)
    eff_debug = build_effective(tmp_path, overrides={"logging.level": "DEBUG"})
    assert eff_info.hash != eff_debug.hash


def test_render_effective_first_line_and_sources(tmp_path: Path) -> None:
    """render_effective produces first line 'EFFECTIVE CONFIG sha256:<12 hex>' and leaf lines."""
    from algodesk.config.snapshot import build_effective, render_effective

    _write_file(
        tmp_path / "config" / "platform.toml",
        'project = "algodesk"\n[logging]\nlevel = "INFO"\nformat = "json"\n',
    )
    env = {"ALGODESK_LOGGING__LEVEL": "DEBUG"}
    eff = build_effective(tmp_path, env=env)
    rendered = render_effective(eff)
    lines = rendered.splitlines()

    # First line format
    expected_first = f"EFFECTIVE CONFIG sha256:{eff.hash.removeprefix('sha256:')[:12]}"
    assert lines[0] == expected_first

    # Check leaf lines have " = " and "   # "
    leaf_lines = lines[1:]
    assert len(leaf_lines) > 0
    for line in leaf_lines:
        assert " = " in line
        assert "   # " in line

    # Check specific line with env override
    assert "logging.level = DEBUG   # env:ALGODESK_LOGGING__LEVEL" in leaf_lines
    assert "project = algodesk   # " in rendered


def test_render_effective_with_run(tmp_path: Path) -> None:
    """render_effective correctly renders run configuration leaves."""
    from algodesk.config.snapshot import build_effective, render_effective

    _write_file(tmp_path / "config" / "platform.toml", 'project = "algodesk"\n')
    _write_file(tmp_path / "config" / "runs" / "demo.toml", _RUN_TOML)

    eff = build_effective(tmp_path, run_name="demo")
    rendered = render_effective(eff)
    assert "allocated_capital = 500000   # " in rendered
    assert "universe.screen = nifty_50   # " in rendered
    assert "data_source.plugin = upstox   # " in rendered


def test_render_effective_secret_reference_safe(tmp_path: Path) -> None:
    """render_effective prints secret:<name> reference safely without leaks."""
    from algodesk.config.snapshot import build_effective, render_effective

    _write_file(tmp_path / "config" / "platform.toml", 'project = "algodesk"\n')
    _write_file(tmp_path / "config" / "runs" / "demo.toml", _RUN_TOML)

    eff = build_effective(
        tmp_path,
        run_name="demo",
        overrides={"data_source.api_key": "secret:upstox-token"},
    )
    rendered = render_effective(eff)
    assert "data_source.api_key = secret:upstox-token   # cli:data_source.api_key" in rendered
