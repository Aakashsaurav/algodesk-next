"""Tests for Step 7: RunConfig backtest section and refusal of order-capable plugins."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from algodesk.api.errors import SafetyViolation
from algodesk.api.kinds import Mode
from algodesk.config.models import (
    BacktestConfig,
    FeesConfig,
    PluginRef,
    RunConfig,
    RunMeta,
    StrategyConfig,
    UniverseConfig,
)
from algodesk.ops.context import build_context
from algodesk.registry.registry import PluginRegistry


def test_backtest_config_validation() -> None:
    # Valid BacktestConfig
    bt = BacktestConfig(
        start="2024-11-10",
        end="2026-11-13",
        fidelity="bar",
        timeframe="1d",
        study="sma_study",
        oos_from="2026-01-01",
    )
    assert bt.fidelity == "bar"
    assert bt.timeframe == "1d"
    assert bt.study == "sma_study"

    # Invalid fidelity
    with pytest.raises(ValidationError, match="fidelity"):
        BacktestConfig(start="2024-11-10", end="2026-11-13", fidelity="tick")

    # start > end
    with pytest.raises(ValidationError, match="start date .* must be <= end date"):
        BacktestConfig(start="2026-11-13", end="2024-11-10")

    # oos_from outside [start, end]
    with pytest.raises(ValidationError, match="oos_from .* must be between start"):
        BacktestConfig(start="2024-11-10", end="2026-11-13", oos_from="2027-01-01")


def test_run_config_requires_backtest_section_in_backtest_mode() -> None:
    # In backtest mode, missing backtest section raises ValidationError
    with pytest.raises(
        ValidationError, match="Backtest mode requires \\[backtest\\] configuration"
    ):
        RunConfig(
            allocated_capital=Decimal("100000"),
            run=RunMeta(name="test_bt", mode=Mode.BACKTEST),
            data_source=PluginRef(plugin="upstox"),
            execution_venue=PluginRef(plugin="sim"),
            calendars=[PluginRef(plugin="exchange")],
            secrets=PluginRef(plugin="keychain"),
            fees=FeesConfig(schedule="zerodha_nse"),
            universe=UniverseConfig(group="demo5"),
            strategies=[
                StrategyConfig(
                    plugin="sma_cross",
                    risk_profile="conservative",
                    params={"timeframe": "1d"},
                )
            ],
            backtest=None,
        )

    # Valid in backtest mode
    rc = RunConfig(
        allocated_capital=Decimal("100000"),
        run=RunMeta(name="test_bt", mode=Mode.BACKTEST),
        data_source=PluginRef(plugin="upstox"),
        execution_venue=PluginRef(plugin="sim"),
        calendars=[PluginRef(plugin="exchange")],
        secrets=PluginRef(plugin="keychain"),
        fees=FeesConfig(schedule="zerodha_nse"),
        universe=UniverseConfig(group="demo5"),
        strategies=[
            StrategyConfig(
                plugin="sma_cross",
                risk_profile="conservative",
                params={"timeframe": "1d"},
            )
        ],
        backtest=BacktestConfig(start="2024-11-10", end="2026-11-13"),
    )
    assert rc.run.mode == Mode.BACKTEST
    assert rc.backtest is not None


def test_order_capable_plugins_refused_in_backtest(tmp_path: Path) -> None:
    from tests.core.registry.conftest import create_fake_plugin

    plugins_dir = tmp_path / "plugins"
    create_fake_plugin(
        plugins_dir,
        "broker",
        "live_broker",
        order_capable=True,
        modes_allowed=["live"],
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    bt_ctx = build_context(Mode.BACKTEST, run_id="bt_run")

    with pytest.raises((SafetyViolation, Exception)) as exc_info:
        reg.create("broker", "live_broker", ctx=bt_ctx)
    err = str(exc_info.value).lower()
    assert "order-capable" in err or "cannot run in mode" in err
