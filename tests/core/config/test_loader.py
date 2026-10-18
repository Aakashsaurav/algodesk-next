"""Tests for algodesk.config.loader — TOML loading, merge layers,
provenance, and error conversion.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from algodesk.api.errors import ConfigError


def _write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_load_toml_success(tmp_path: Path) -> None:
    """load_toml parses valid TOML into a dictionary."""
    from algodesk.config.loader import load_toml

    file = _write_file(tmp_path / "test.toml", 'key = "value"\n[section]\nnum = 42\n')
    data = load_toml(file)
    assert data == {"key": "value", "section": {"num": 42}}


def test_load_toml_missing_file_raises_config_error(tmp_path: Path) -> None:
    """Missing file raises ConfigError naming file and 'file' key."""
    from algodesk.config.loader import load_toml

    missing = tmp_path / "nonexistent.toml"
    with pytest.raises(ConfigError) as exc_info:
        load_toml(missing)
    err = exc_info.value
    assert str(missing) in err.where
    assert "file" in err.where
    assert "not found" in err.what


def test_load_toml_syntax_error_names_file_and_line(tmp_path: Path) -> None:
    """Syntax error in TOML raises ConfigError with file, line, and fix."""
    from algodesk.config.loader import load_toml

    bad_file = _write_file(tmp_path / "broken.toml", "a = 1\nb = @bad_syntax\n")
    with pytest.raises(ConfigError) as exc_info:
        load_toml(bad_file)
    err = exc_info.value
    assert str(bad_file) in err.where
    assert ":2" in err.where or "line 2" in err.what
    assert "syntax error" in err.what.lower()
    assert len(err.fix) > 0


def test_deep_merge_keeps_untouched_nested_keys() -> None:
    """deep_merge preserves base keys not touched by override."""
    from algodesk.config.loader import deep_merge

    base = {"paths": {"data_dir": "data", "logs_dir": "logs"}}
    override = {"paths": {"logs_dir": "custom_logs"}}
    merged = deep_merge(base, override)
    assert merged == {"paths": {"data_dir": "data", "logs_dir": "custom_logs"}}


def test_deep_merge_replaces_lists() -> None:
    """Lists are replaced, not concatenated."""
    from algodesk.config.loader import deep_merge

    base = {"plugin_dirs": ["plugins", "core_plugins"]}
    override = {"plugin_dirs": ["custom_plugins"]}
    merged = deep_merge(base, override)
    assert merged == {"plugin_dirs": ["custom_plugins"]}


def test_deep_merge_replaces_scalars() -> None:
    """Scalars are replaced by override."""
    from algodesk.config.loader import deep_merge

    base = {"project": "old", "count": 10}
    override = {"project": "new"}
    merged = deep_merge(base, override)
    assert merged == {"project": "new", "count": 10}


def test_deep_merge_does_not_mutate_inputs() -> None:
    """deep_merge leaves input mappings unmodified."""
    from algodesk.config.loader import deep_merge

    base = {"nested": {"a": [1, 2]}}
    override = {"nested": {"b": 3}}
    base_copy = {"nested": {"a": [1, 2]}}
    override_copy = {"nested": {"b": 3}}
    deep_merge(base, override)
    assert base == base_copy
    assert override == override_copy


def test_merge_layers_provenance_points_to_right_file() -> None:
    """merge_layers correctly maps each leaf key to its layer source."""
    from algodesk.config.loader import merge_layers

    layers = [
        ("file1.toml", {"project": "proj1", "paths": {"data_dir": "d1", "logs_dir": "l1"}}),
        ("file2.toml", {"paths": {"logs_dir": "l2"}}),
        ("file3.toml", {"paths": {"data_dir": "d3"}, "extra": "e3"}),
    ]
    merged, provenance = merge_layers(layers)
    assert merged["project"] == "proj1"
    assert merged["paths"]["data_dir"] == "d3"
    assert merged["paths"]["logs_dir"] == "l2"
    assert merged["extra"] == "e3"

    assert provenance["project"] == "file1.toml"
    assert provenance["paths.data_dir"] == "file3.toml"
    assert provenance["paths.logs_dir"] == "file2.toml"
    assert provenance["extra"] == "file3.toml"


def test_load_platform_defaults_with_no_files(tmp_path: Path) -> None:
    """load_platform with no platform.toml returns default PlatformConfig."""
    from algodesk.config.loader import load_platform

    cfg = load_platform(tmp_path)
    assert cfg.project == "algodesk"
    assert cfg.paths.data_dir == "data"
    assert cfg.logging.level == "INFO"
    assert cfg.plugin_dirs == ["plugins"]


def test_load_platform_precedence_platform_and_profile(tmp_path: Path) -> None:
    """Profile overrides platform configuration."""
    from algodesk.config.loader import load_platform

    _write_file(
        tmp_path / "config" / "platform.toml",
        'project = "base_proj"\n[logging]\nlevel = "WARNING"\nformat = "json"\n',
    )
    _write_file(
        tmp_path / "config" / "profiles" / "dev.toml",
        '[logging]\nlevel = "DEBUG"\n',
    )
    cfg = load_platform(tmp_path, profile="dev")
    assert cfg.project == "base_proj"
    assert cfg.logging.level == "DEBUG"
    assert cfg.logging.format == "json"


def test_precedence_platform_profile_run(tmp_path: Path) -> None:
    """Precedence is platform < profile < run."""
    from algodesk.config.loader import load_run

    _write_file(
        tmp_path / "config" / "platform.toml",
        '[deployment]\nprofile = "from_platform"\n',
    )
    _write_file(
        tmp_path / "config" / "profiles" / "prod.toml",
        '[deployment]\nprofile = "from_profile"\n',
    )
    run_toml = """
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
[universe]
screen = "nifty_50"
[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
"""
    _write_file(tmp_path / "config" / "runs" / "demo.toml", run_toml)
    cfg = load_run(tmp_path, run_name="demo", profile="prod")
    assert cfg.deployment.profile == "from_profile"
    assert cfg.allocated_capital == Decimal("500000")


def test_missing_profile_raises_config_error(tmp_path: Path) -> None:
    """Missing named profile raises ConfigError naming file and 'profile'."""
    from algodesk.config.loader import load_platform

    with pytest.raises(ConfigError) as exc_info:
        load_platform(tmp_path, profile="nonexistent")
    err = exc_info.value
    assert "nonexistent.toml" in err.where
    assert "profile" in err.where
    assert "not found" in err.what


def test_missing_run_file_raises_config_error(tmp_path: Path) -> None:
    """Missing run file raises ConfigError naming file and 'run'."""
    from algodesk.config.loader import load_run

    with pytest.raises(ConfigError) as exc_info:
        load_run(tmp_path, run_name="missing_run")
    err = exc_info.value
    assert "missing_run.toml" in err.where
    assert "run" in err.where
    assert "not found" in err.what


def test_path_separator_in_run_name_rejected(tmp_path: Path) -> None:
    """Run name with path traversal or separators is rejected."""
    from algodesk.config.loader import load_run

    with pytest.raises(ConfigError) as exc_info:
        load_run(tmp_path, run_name="../escape")
    err = exc_info.value
    assert "run.name" in err.where
    assert "path separators" in err.what

    with pytest.raises(ConfigError) as exc_info2:
        load_run(tmp_path, run_name="sub/run")
    assert "run.name" in exc_info2.value.where


def test_path_separator_in_profile_name_rejected(tmp_path: Path) -> None:
    """Profile name with path traversal or separators is rejected."""
    from algodesk.config.loader import load_platform

    with pytest.raises(ConfigError) as exc_info:
        load_platform(tmp_path, profile="sub/profile")
    assert "profile.name" in exc_info.value.where


def test_unknown_key_raises_config_error_with_allowed_keys(tmp_path: Path) -> None:
    """Unknown key in configuration raises ConfigError showing allowed keys."""
    from algodesk.config.loader import load_platform

    _write_file(
        tmp_path / "config" / "platform.toml",
        '[paths]\ndata_dir = "data"\nunknown_field = "bad"\n',
    )
    with pytest.raises(ConfigError) as exc_info:
        load_platform(tmp_path)
    err = exc_info.value
    assert "platform.toml" in err.where
    assert "paths.unknown_field" in err.where
    assert "unknown_field" in err.what
    assert "Allowed keys:" in err.fix
    assert "data_dir" in err.fix


def test_wrong_type_raises_config_error_with_expected(tmp_path: Path) -> None:
    """Wrong data type raises ConfigError showing expected type."""
    from algodesk.config.loader import load_platform

    _write_file(
        tmp_path / "config" / "platform.toml",
        "[logging]\nlevel = 9999\n",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_platform(tmp_path)
    err = exc_info.value
    assert "platform.toml" in err.where
    assert "logging.level" in err.where
    assert "Invalid type" in err.what
    assert "Expected" in err.fix


def test_bad_enum_raises_config_error_with_allowed_values(tmp_path: Path) -> None:
    """Invalid enum value raises ConfigError showing allowed enum values."""
    from algodesk.config.loader import load_run

    run_toml = """
allocated_capital = 100000
[run]
name = "demo"
mode = "super_live"
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
[universe]
screen = "nifty_50"
[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
"""
    _write_file(tmp_path / "config" / "runs" / "bad_enum.toml", run_toml)
    with pytest.raises(ConfigError) as exc_info:
        load_run(tmp_path, run_name="bad_enum")
    err = exc_info.value
    assert "bad_enum.toml" in err.where
    assert "run.mode" in err.where
    assert "Allowed values:" in err.fix
    assert "'live'" in err.fix or "research" in err.fix


def test_capital_le_zero_raises_config_error(tmp_path: Path) -> None:
    """allocated_capital <= 0 raises ConfigError."""
    from algodesk.config.loader import load_run

    run_toml = """
allocated_capital = 0
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
[universe]
screen = "nifty_50"
[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
"""
    _write_file(tmp_path / "config" / "runs" / "zero_cap.toml", run_toml)
    with pytest.raises(ConfigError) as exc_info:
        load_run(tmp_path, run_name="zero_cap")
    err = exc_info.value
    assert "zero_cap.toml" in err.where
    assert "allocated_capital" in err.where
    assert "greater than 0" in err.what


def test_universe_both_or_neither_raises_config_error(tmp_path: Path) -> None:
    """Universe with both screen and symbols or neither raises ConfigError."""
    from algodesk.config.loader import load_run

    run_both = """
allocated_capital = 100000
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
[universe]
screen = "nifty_50"
symbols = ["RELIANCE", "TCS"]
[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
"""
    _write_file(tmp_path / "config" / "runs" / "both_uni.toml", run_both)
    with pytest.raises(ConfigError) as exc_info:
        load_run(tmp_path, run_name="both_uni")
    err = exc_info.value
    assert "both_uni.toml" in err.where
    assert "universe" in err.where
    assert "exactly one" in err.what

    run_neither = """
allocated_capital = 100000
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
[universe]
[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
"""
    _write_file(tmp_path / "config" / "runs" / "neither_uni.toml", run_neither)
    with pytest.raises(ConfigError) as exc_info2:
        load_run(tmp_path, run_name="neither_uni")
    assert "universe" in exc_info2.value.where


def test_empty_strategies_raises_config_error(tmp_path: Path) -> None:
    """Run without any strategy entries raises ConfigError."""
    from algodesk.config.loader import load_run

    run_empty = """
allocated_capital = 100000
strategies = []
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
[universe]
screen = "nifty_50"
"""
    _write_file(tmp_path / "config" / "runs" / "empty_strat.toml", run_empty)
    with pytest.raises(ConfigError) as exc_info:
        load_run(tmp_path, run_name="empty_strat")
    err = exc_info.value
    assert "empty_strat.toml" in err.where
    assert "strategies" in err.where
    assert "At least one strategy" in err.what


def test_every_config_error_contains_file_name_and_key(tmp_path: Path) -> None:
    """Every ConfigError raised by loader contains both file name and key in str()."""
    from algodesk.config.loader import load_platform, load_run

    # 1. Missing file
    with pytest.raises(ConfigError) as exc1:
        load_run(tmp_path, run_name="nonexistent")
    assert "nonexistent.toml" in str(exc1.value)
    assert "run" in str(exc1.value)

    # 2. Syntax error
    _write_file(tmp_path / "config" / "platform.toml", "bad = [")
    with pytest.raises(ConfigError) as exc2:
        load_platform(tmp_path)
    assert "platform.toml" in str(exc2.value)

    # 3. Validation error
    _write_file(tmp_path / "config" / "platform.toml", "project = 123\n")
    with pytest.raises(ConfigError) as exc3:
        load_platform(tmp_path)
    assert "platform.toml" in str(exc3.value)
    assert "project" in str(exc3.value)
