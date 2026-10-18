"""Tests for environment variable and CLI configuration overrides."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from algodesk.api.errors import ConfigError

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


def test_env_precedence_over_platform(tmp_path: Path) -> None:
    """Environment layer overrides platform.toml setting."""
    from algodesk.config.loader import load_platform

    _write_file(
        tmp_path / "config" / "platform.toml",
        'project = "from_file"\n[logging]\nlevel = "INFO"\n',
    )
    env = {"ALGODESK_PROJECT": "from_env"}
    cfg = load_platform(tmp_path, env=env)
    assert cfg.project == "from_env"


def test_env_precedence_over_profile(tmp_path: Path) -> None:
    """Environment layer overrides profile setting."""
    from algodesk.config.loader import load_platform

    _write_file(
        tmp_path / "config" / "platform.toml",
        'project = "from_platform"\n[logging]\nlevel = "INFO"\n',
    )
    _write_file(
        tmp_path / "config" / "profiles" / "prod.toml",
        '[logging]\nlevel = "WARNING"\n',
    )
    env = {"ALGODESK_LOGGING__LEVEL": "ERROR"}
    cfg = load_platform(tmp_path, profile="prod", env=env)
    assert cfg.logging.level == "ERROR"


def test_env_precedence_over_run(tmp_path: Path) -> None:
    """Environment layer overrides run.toml setting."""
    from algodesk.config.loader import load_run

    _write_file(tmp_path / "config" / "runs" / "demo.toml", _RUN_TOML)
    env = {"ALGODESK_ALLOCATED_CAPITAL": "750000"}
    cfg = load_run(tmp_path, run_name="demo", env=env)
    assert cfg.allocated_capital == Decimal("750000")


def test_cli_wins_over_env_and_files(tmp_path: Path) -> None:
    """CLI override layer wins over env, run, profile, and platform."""
    from algodesk.config.loader import load_platform

    _write_file(
        tmp_path / "config" / "platform.toml",
        '[logging]\nlevel = "INFO"\n',
    )
    env = {"ALGODESK_LOGGING__LEVEL": "WARNING"}
    overrides = {"logging.level": "DEBUG"}
    cfg = load_platform(tmp_path, env=env, overrides=overrides)
    assert cfg.logging.level == "DEBUG"


def test_precedence_full_stack(tmp_path: Path) -> None:
    """Precedence stack: platform < profile < run < env < cli."""
    from algodesk.config.loader import load_run

    _write_file(
        tmp_path / "config" / "platform.toml",
        '[deployment]\nprofile = "platform_profile"\n',
    )
    _write_file(
        tmp_path / "config" / "profiles" / "custom.toml",
        '[deployment]\nprofile = "profile_profile"\n',
    )
    _write_file(tmp_path / "config" / "runs" / "demo.toml", _RUN_TOML)

    # 1. Run file overrides profile and platform
    cfg1 = load_run(tmp_path, run_name="demo", profile="custom", env={})
    assert cfg1.deployment.profile == "local"

    # 2. Env overrides run
    env = {"ALGODESK_DEPLOYMENT__PROFILE": "env_profile"}
    cfg2 = load_run(tmp_path, run_name="demo", profile="custom", env=env)
    assert cfg2.deployment.profile == "env_profile"

    # 3. CLI overrides env
    overrides = {"deployment.profile": "cli_profile"}
    cfg3 = load_run(tmp_path, run_name="demo", profile="custom", env=env, overrides=overrides)
    assert cfg3.deployment.profile == "cli_profile"


def test_env_nesting_double_underscore(tmp_path: Path) -> None:
    """Double underscore serves as nesting separator."""
    from algodesk.config.loader import load_platform

    _write_file(
        tmp_path / "config" / "platform.toml",
        '[paths]\ndata_dir = "orig_data"\nlogs_dir = "orig_logs"\n',
    )
    env = {
        "ALGODESK_PATHS__DATA_DIR": "new_data",
        "ALGODESK_PATHS__LOGS_DIR": "new_logs",
    }
    cfg = load_platform(tmp_path, env=env)
    assert cfg.paths.data_dir == "new_data"
    assert cfg.paths.logs_dir == "new_logs"


def test_env_parsing_scalar_int(tmp_path: Path) -> None:
    """Integer value in env is parsed as scalar int."""
    from algodesk.config.loader import load_run

    _write_file(tmp_path / "config" / "runs" / "demo.toml", _RUN_TOML)
    env = {"ALGODESK_ALLOCATED_CAPITAL": "1000000"}
    cfg = load_run(tmp_path, run_name="demo", env=env)
    assert cfg.allocated_capital == Decimal("1000000")


def test_env_parsing_bool(tmp_path: Path) -> None:
    """Boolean values in env are parsed as bool scalars."""
    from algodesk.config.loader import load_run

    _write_file(tmp_path / "config" / "runs" / "demo.toml", _RUN_TOML)
    env = {"ALGODESK_DATA_SOURCE__SANDBOX": "true"}
    cfg = load_run(tmp_path, run_name="demo", env=env)
    assert cfg.data_source.model_extra is not None
    assert cfg.data_source.model_extra["sandbox"] is True


def test_env_parsing_list(tmp_path: Path) -> None:
    """JSON/TOML array in env is parsed as a Python list."""
    from algodesk.config.loader import load_platform

    _write_file(
        tmp_path / "config" / "platform.toml",
        'plugin_dirs = ["plugins"]\n',
    )
    env = {"ALGODESK_PLUGIN_DIRS": '["extra_plugins", "vendor_plugins"]'}
    cfg = load_platform(tmp_path, env=env)
    assert cfg.plugin_dirs == ["extra_plugins", "vendor_plugins"]


def test_env_parsing_string_fallback(tmp_path: Path) -> None:
    """Bare unquoted strings fall back to plain str."""
    from algodesk.config.loader import load_platform

    _write_file(
        tmp_path / "config" / "platform.toml",
        'project = "algodesk"\n',
    )
    env = {"ALGODESK_PROJECT": "custom-workbench"}
    cfg = load_platform(tmp_path, env=env)
    assert cfg.project == "custom-workbench"


def test_env_unknown_key_names_variable(tmp_path: Path) -> None:
    """Unknown root env variable fails and names the environment variable."""
    from algodesk.config.loader import load_platform

    _write_file(tmp_path / "config" / "platform.toml", "project = 'test'\n")
    env = {"ALGODESK_NONEXISTENT": "val"}
    with pytest.raises(ConfigError) as exc_info:
        load_platform(tmp_path, env=env)
    err = exc_info.value
    assert "Unknown configuration key 'nonexistent'" in err.what
    assert "env:ALGODESK_NONEXISTENT" in err.where


def test_env_unknown_nested_key_names_variable(tmp_path: Path) -> None:
    """Unknown nested env variable fails and names the environment variable."""
    from algodesk.config.loader import load_platform

    _write_file(tmp_path / "config" / "platform.toml", "project = 'test'\n")
    env = {"ALGODESK_LOGGING__NONEXISTENT": "val"}
    with pytest.raises(ConfigError) as exc_info:
        load_platform(tmp_path, env=env)
    err = exc_info.value
    assert "Unknown configuration key 'nonexistent'" in err.what
    assert "env:ALGODESK_LOGGING__NONEXISTENT" in err.where


def test_env_reserved_variable_ignored(tmp_path: Path) -> None:
    """Variables exported by scripts/env.sh are ignored even if prefixed with ALGODESK_."""
    from algodesk.config.loader import load_platform

    _write_file(
        tmp_path / "config" / "platform.toml",
        'project = "algodesk"\n',
    )
    env = {
        "ROOT": "/custom/root",
        "ALGODESK_ROOT": "/custom/algodesk/root",
        "UV_CACHE_DIR": "/custom/cache",
        "ALGODESK_UV_CACHE_DIR": "/custom/cache2",
        "PRE_COMMIT_HOME": "/custom/pre-commit",
    }
    cfg = load_platform(tmp_path, env=env)
    assert cfg.project == "algodesk"


def test_provenance_labels_for_env_and_cli(tmp_path: Path) -> None:
    """Provenance accurately records 'env:<VAR>' and 'cli:<dotted.key>'."""
    from algodesk.config.loader import load_platform_with_provenance

    _write_file(
        tmp_path / "config" / "platform.toml",
        'project = "algodesk"\n[logging]\nlevel = "INFO"\nformat = "json"\n',
    )
    env = {"ALGODESK_LOGGING__LEVEL": "DEBUG"}
    overrides = {"logging.format": "text"}
    _, prov = load_platform_with_provenance(tmp_path, env=env, overrides=overrides)
    assert prov["logging.level"] == "env:ALGODESK_LOGGING__LEVEL"
    assert prov["logging.format"] == "cli:logging.format"
    assert "platform.toml" in prov["project"]


def test_env_type_mismatch_error(tmp_path: Path) -> None:
    """Type mismatch in env variable raises ConfigError naming env var."""
    from algodesk.config.loader import load_platform

    _write_file(tmp_path / "config" / "platform.toml", 'project = "algodesk"\n')
    env = {"ALGODESK_LOGGING__LEVEL": "123"}
    with pytest.raises(ConfigError) as exc_info:
        load_platform(tmp_path, env=env)
    err = exc_info.value
    assert "Invalid type for 'logging.level'" in err.what
    assert "env:ALGODESK_LOGGING__LEVEL" in err.where
