"""Tests for secret rule, path validation rule, and directory creation."""

from __future__ import annotations

from pathlib import Path

import pytest

from algodesk.api.errors import ConfigError

_BASE_RUN_PREFIX = """
allocated_capital = 500000
[run]
name = "demo"
mode = "paper"
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


# --- Secret Rule Tests ---


@pytest.mark.parametrize(
    "key_name",
    [
        "auth_token",
        "client_secret",
        "db_password",
        "user_passwd",
        "api_key",
        "broker_apikey",
        "private_key",
        "user_credential",
    ],
)
def test_secret_rule_plain_secret_keywords_rejected(tmp_path: Path, key_name: str) -> None:
    """Keys with secret keywords must use secret:<name> references, plain rejected."""
    from algodesk.config.loader import load_run

    secret_value = "super_secret_value_123"
    run_toml = (
        f'{_BASE_RUN_PREFIX}\n[data_source]\nplugin = "upstox"\n{key_name} = "{secret_value}"\n'
    )
    _write_file(tmp_path / "config" / "runs" / "demo.toml", run_toml)

    with pytest.raises(ConfigError) as exc_info:
        load_run(tmp_path, run_name="demo")
    err = exc_info.value
    assert err.what == "plain secret in config"
    assert key_name in err.where
    assert "store it with your secrets provider" in err.fix
    # Value must NEVER appear in message, repr, or str
    assert secret_value not in str(err)
    assert secret_value not in repr(err)


def test_secret_rule_reference_accepted(tmp_path: Path) -> None:
    """Valid reference 'secret:<name>' is accepted."""
    from algodesk.config.loader import load_run

    run_toml = (
        _BASE_RUN_PREFIX
        + '\n[data_source]\nplugin = "upstox"\napi_key = "secret:valid_token-123.prod"\n'
    )
    _write_file(tmp_path / "config" / "runs" / "demo.toml", run_toml)
    cfg = load_run(tmp_path, run_name="demo")
    assert cfg.data_source.model_extra is not None
    assert cfg.data_source.model_extra["api_key"] == "secret:valid_token-123.prod"


@pytest.mark.parametrize(
    "bad_ref",
    [
        "secret:",  # empty name
        "secret:bad/slash",  # invalid slash
        "secret:bad space",  # invalid space
        "secret:bad*char",  # invalid asterisk
        "secret:" + ("a" * 65),  # name too long (> 64 chars)
    ],
)
def test_secret_rule_bad_reference_rejected(tmp_path: Path, bad_ref: str) -> None:
    """Invalid reference patterns are rejected."""
    from algodesk.config.loader import load_run

    run_toml = _BASE_RUN_PREFIX + f'\n[data_source]\nplugin = "upstox"\napi_key = "{bad_ref}"\n'
    _write_file(tmp_path / "config" / "runs" / "demo.toml", run_toml)

    with pytest.raises(ConfigError) as exc_info:
        load_run(tmp_path, run_name="demo")
    err = exc_info.value
    assert err.what == "plain secret in config"


def test_secret_rule_long_base64_string_rejected(tmp_path: Path) -> None:
    """Any non-secret key containing 40+ base64 characters is rejected."""
    from algodesk.config.loader import load_run

    long_b64 = "A" * 40
    run_toml = (
        _BASE_RUN_PREFIX + f'\n[data_source]\nplugin = "upstox"\ncustom_field = "{long_b64}"\n'
    )
    _write_file(tmp_path / "config" / "runs" / "demo.toml", run_toml)

    with pytest.raises(ConfigError) as exc_info:
        load_run(tmp_path, run_name="demo")
    err = exc_info.value
    assert err.what == "plain secret in config"
    assert "custom_field" in err.where
    assert long_b64 not in str(err)
    assert long_b64 not in repr(err)


def test_secret_rule_in_list_rejected(tmp_path: Path) -> None:
    """Plain secret string inside a list is rejected."""
    from algodesk.config.loader import load_run

    run_toml = (
        _BASE_RUN_PREFIX
        + '\n[data_source]\nplugin = "upstox"\ntokens = ["secret:ok", "plain_leak"]\n'
    )
    _write_file(tmp_path / "config" / "runs" / "demo.toml", run_toml)

    with pytest.raises(ConfigError) as exc_info:
        load_run(tmp_path, run_name="demo")
    err = exc_info.value
    assert err.what == "plain secret in config"
    assert "plain_leak" not in str(err)


# --- Path Rule Tests ---


def test_path_rule_inside_accepted(tmp_path: Path) -> None:
    """Valid paths inside root resolve to absolute ResolvedPaths."""
    from algodesk.config.loader import ResolvedPaths, resolve_paths
    from algodesk.config.models import PathsConfig, PlatformConfig

    cfg = PlatformConfig(
        paths=PathsConfig(
            data_dir="my_data",
            logs_dir="my_logs",
            results_dir="my_results",
            local_dir=".my_local",
        ),
        plugin_dirs=["plugins", "vendor/plugins"],
    )
    resolved = resolve_paths(tmp_path, cfg)
    assert isinstance(resolved, ResolvedPaths)
    assert resolved.data == (tmp_path / "my_data").resolve()
    assert resolved.logs == (tmp_path / "my_logs").resolve()
    assert resolved.results == (tmp_path / "my_results").resolve()
    assert resolved.local == (tmp_path / ".my_local").resolve()
    assert len(resolved.plugin_dirs) == 2
    assert resolved.plugin_dirs[0] == (tmp_path / "plugins").resolve()


def test_path_rule_dotdot_escape_rejected_cites_nfr14(tmp_path: Path) -> None:
    """Paths containing '..' traversal are rejected citing NFR-14."""
    from algodesk.config.loader import resolve_paths
    from algodesk.config.models import PlatformConfig

    # PathsConfig forbids '..', test via plugin_dirs
    cfg = PlatformConfig(plugin_dirs=["../escape_dir"])
    with pytest.raises(ConfigError) as exc_info:
        resolve_paths(tmp_path, cfg)
    err = exc_info.value
    assert "NFR-14" in err.what
    assert "plugin_dirs" in err.where


def test_path_rule_absolute_outside_rejected_cites_nfr14(tmp_path: Path) -> None:
    """Absolute paths outside workspace root are rejected citing NFR-14."""
    from algodesk.config.loader import resolve_paths
    from algodesk.config.models import PlatformConfig

    outside_dir = Path("/tmp/outside_workspace_test")
    cfg = PlatformConfig(plugin_dirs=[str(outside_dir)])
    with pytest.raises(ConfigError) as exc_info:
        resolve_paths(tmp_path, cfg)
    err = exc_info.value
    assert "NFR-14" in err.what
    assert "plugin_dirs" in err.where


def test_path_rule_symlink_outside_rejected_cites_nfr14(tmp_path: Path) -> None:
    """Symlinks that leave the workspace root are rejected citing NFR-14."""
    from algodesk.config.loader import resolve_paths
    from algodesk.config.models import PlatformConfig

    outside_dir = tmp_path.parent / "symlink_target_outside"
    outside_dir.mkdir(parents=True, exist_ok=True)
    symlink_dir = tmp_path / "symlink_dir"
    symlink_dir.symlink_to(outside_dir)

    cfg = PlatformConfig(plugin_dirs=["symlink_dir"])
    with pytest.raises(ConfigError) as exc_info:
        resolve_paths(tmp_path, cfg)
    err = exc_info.value
    assert "NFR-14" in err.what
    assert "plugin_dirs" in err.where


def test_path_rule_root_itself_rejected(tmp_path: Path) -> None:
    """Path that resolves to workspace root itself is rejected."""
    from algodesk.config.loader import resolve_paths
    from algodesk.config.models import PlatformConfig

    cfg = PlatformConfig(plugin_dirs=["."])
    with pytest.raises(ConfigError) as exc_info:
        resolve_paths(tmp_path, cfg)
    err = exc_info.value
    assert "NFR-14" in err.what


# --- Ensure Dirs Tests ---


def test_ensure_dirs_creates_mode_700_and_idempotent(tmp_path: Path) -> None:
    """ensure_dirs creates data, logs, results, local with 0o700 mode idempotently."""
    from algodesk.config.loader import ensure_dirs, resolve_paths
    from algodesk.config.models import PathsConfig, PlatformConfig

    cfg = PlatformConfig(
        paths=PathsConfig(
            data_dir="data",
            logs_dir="logs",
            results_dir="results",
            local_dir=".local",
        ),
        plugin_dirs=["plugins"],
    )
    resolved = resolve_paths(tmp_path, cfg)

    # 1. First run creates dirs
    ensure_dirs(resolved)
    for p in (resolved.data, resolved.logs, resolved.results, resolved.local):
        assert p.is_dir()
        assert (p.stat().st_mode & 0o777) == 0o700

    # 2. Second run is idempotent and maintains 0o700
    ensure_dirs(resolved)
    for p in (resolved.data, resolved.logs, resolved.results, resolved.local):
        assert p.is_dir()
        assert (p.stat().st_mode & 0o777) == 0o700
