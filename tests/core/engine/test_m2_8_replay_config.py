"""Tests for M2-8 CP1 Step 4: Replay recorded configuration loading, refusal, and reproduction."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from algodesk.api.errors import ConfigError
from algodesk.cli.common import _open_rt
from algodesk.engine.replay_config import (
    apply_recorded_registry_snapshot,
    check_session_manifest_warnings,
    load_recorded_session_config,
    parse_recorded_config,
)
from algodesk.params.registry import ParamRegistry

_SAMPLE_CONFIG_TXT = (
    "allocated_capital=Decimal('100000') "
    "run=RunMeta(name='demo_test', mode=<Mode.PAPER: 'paper'>) "
    "data_source=PluginRef(plugin='upstox', auth_priority=['analytics', 'oauth']) "
    "execution_venue=PluginRef(plugin='sim', seed=7) "
    "calendars=[PluginRef(plugin='exchange', exchange='nse')] "
    "secrets=PluginRef(plugin='keychain') "
    "fees=FeesConfig(schedule='zerodha_nse') "
    "deployment=DeploymentConfig(profile='local') "
    "universe=UniverseConfig(screen=None, symbols=['NSE:EQ:RELIANCE'], group=None) "
    "strategies=[StrategyConfig(plugin='sma_cross', risk_profile='conservative', "
    "params={'timeframe': '1m', 'fast': 3, 'slow': 8, 'qty': 1, 'exit_time': '21:15'})] "
    "notifiers=[PluginRef(plugin='telegram')]"
)


def test_parse_recorded_config_success() -> None:
    """parse_recorded_config successfully parses effective text into RunConfig."""
    cfg = parse_recorded_config(_SAMPLE_CONFIG_TXT)
    assert cfg.run.name == "demo_test"
    assert cfg.allocated_capital == Decimal("100000")
    assert cfg.fees.schedule == "zerodha_nse"
    assert len(cfg.strategies) == 1
    assert cfg.strategies[0].params["fast"] == 3
    # Defaults missing product to delivery ("cnc")
    assert cfg.strategies[0].params["product"] == "cnc"


def test_parse_recorded_config_preserves_explicit_product() -> None:
    """Explicit product in params (e.g. MIS) is preserved during parse."""
    txt = _SAMPLE_CONFIG_TXT.replace("'qty': 1", "'qty': 1, 'product': 'MIS'")
    cfg = parse_recorded_config(txt)
    assert cfg.strategies[0].params["product"] == "MIS"


def test_parse_recorded_config_empty_refuses() -> None:
    """Empty config.effective.txt raises ConfigError."""
    with pytest.raises(ConfigError, match="Recorded config.effective.txt is empty"):
        parse_recorded_config("")


def test_apply_recorded_registry_snapshot_success(tmp_project: Path) -> None:
    """Recorded parameters from snapshot are applied to ParamRegistry."""
    snap_file = tmp_project / "registry.snapshot.json"
    snap_data = {
        "params": {
            "corp.price_verify_tolerance_pct": 7.5,
            "data.closure_max_pct": 12.0,
        }
    }
    snap_file.write_text(json.dumps(snap_data), encoding="utf-8")

    reg = ParamRegistry(tmp_project / "params.db")
    reg.load_defaults(Path(__file__).parents[3] / "config" / "params")

    apply_recorded_registry_snapshot(snap_file, reg)
    assert reg.get("corp.price_verify_tolerance_pct") == 7.5
    assert reg.get("data.closure_max_pct") == 12.0
    reg.close()


def test_apply_recorded_registry_snapshot_unknown_param_refuses(tmp_project: Path) -> None:
    """Recorded unknown parameter in snapshot refuses and names the parameter."""
    snap_file = tmp_project / "registry.snapshot.json"
    snap_data = {"params": {"nonexistent.param.foo": 123}}
    snap_file.write_text(json.dumps(snap_data), encoding="utf-8")

    reg = ParamRegistry(tmp_project / "params.db")
    reg.load_defaults(Path(__file__).parents[3] / "config" / "params")

    with pytest.raises(ConfigError) as exc_info:
        apply_recorded_registry_snapshot(snap_file, reg)
    assert "nonexistent.param.foo" in exc_info.value.what
    assert "not registered" in exc_info.value.what
    reg.close()


def test_apply_recorded_registry_snapshot_invalid_value_refuses(tmp_project: Path) -> None:
    """Out-of-bounds parameter value in snapshot refuses and names parameter."""
    snap_file = tmp_project / "registry.snapshot.json"
    # corp.price_verify_tolerance_pct max is 20.0; 999.0 is invalid
    snap_data = {"params": {"corp.price_verify_tolerance_pct": 999.0}}
    snap_file.write_text(json.dumps(snap_data), encoding="utf-8")

    reg = ParamRegistry(tmp_project / "params.db")
    reg.load_defaults(Path(__file__).parents[3] / "config" / "params")

    with pytest.raises(ConfigError) as exc_info:
        apply_recorded_registry_snapshot(snap_file, reg)
    assert "corp.price_verify_tolerance_pct" in exc_info.value.what
    assert "cannot be applied" in exc_info.value.what
    reg.close()


def test_check_session_manifest_warnings_detects_hash_diff() -> None:
    """Plugin hash differences generate explicit warnings."""
    mock_rt = MagicMock()
    p_mock = MagicMock()
    p_mock.name = "sma_cross"
    p_mock.code_hash = "current_hash_123"
    p_mock.manifest.version = "0.1.0"
    mock_rt.plugins.list.return_value = [p_mock]
    mock_rt.plugins.info.return_value = p_mock

    cfg = parse_recorded_config(_SAMPLE_CONFIG_TXT)

    manifest_data = {
        "code_version": "0.1.0",
        "plugin_versions": {"sma_cross": {"version": "0.1.0", "hash": "recorded_old_hash_456"}},
    }
    tmp_man = Path("/tmp/test_manifest.json")
    tmp_man.write_text(json.dumps(manifest_data), encoding="utf-8")

    warnings = check_session_manifest_warnings(tmp_man, mock_rt, cfg)
    assert any("sma_cross" in w and "code hash" in w for w in warnings)


def test_check_session_manifest_missing_fee_schedule_refuses() -> None:
    """Recorded fee schedule not installed refuses with ConfigError."""
    mock_rt = MagicMock()
    mock_rt.plugins.info.side_effect = Exception("Not found")

    cfg = parse_recorded_config(_SAMPLE_CONFIG_TXT)
    with pytest.raises(ConfigError, match="Recorded fee schedule 'zerodha_nse' is not installed"):
        check_session_manifest_warnings(Path("/nonexistent"), mock_rt, cfg)


def test_replay_reproduces_paper_result_after_run_config_change(tmp_project: Path) -> None:
    """Replay loads recorded config and reproduces result even after config/runs/ changes."""
    sess_dir = tmp_project / "results" / "test_paper_sess_123"
    sess_dir.mkdir(parents=True, exist_ok=True)

    # Write recorded config.effective.txt with fast=5, slow=10, product='CNC'
    cfg_txt = _SAMPLE_CONFIG_TXT.replace(
        "'fast': 3, 'slow': 8", "'fast': 5, 'slow': 10, 'product': 'CNC'"
    )
    (sess_dir / "config.effective.txt").write_text(cfg_txt, encoding="utf-8")

    class DummyCtx:
        obj = None

    with _open_rt(DummyCtx(), ensure=True) as rt:
        # Load recorded config for session
        loaded_cfg, warnings = load_recorded_session_config(sess_dir, rt)
        assert loaded_cfg.strategies[0].params["fast"] == 5
        assert loaded_cfg.strategies[0].params["slow"] == 10
        assert loaded_cfg.strategies[0].params["product"] == "CNC"
