"""Unit and integration tests for ADR-084 targeted fee readiness verification."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from algodesk.cli.app import app
from algodesk.config.models import (
    BacktestConfig,
    FeesConfig,
    RunConfig,
    StrategyConfig,
    UniverseConfig,
)
from algodesk.ops.fees import check_fees_readiness
from algodesk.ops.runtime import open_runtime

runner = CliRunner()


def test_fees_readiness_equity_cnc_ok(tmp_path: Path) -> None:
    """Equity delivery run starting after 2020-01-01 passes fee readiness with OK."""
    with open_runtime(root=Path.cwd(), ensure=False) as rt:
        run_cfg = RunConfig(
            allocated_capital=500000,
            run={"name": "test_eq", "mode": "backtest"},
            backtest=BacktestConfig(start="2024-11-10", end="2025-01-10", timeframe="1d"),
            data_source={"plugin": "upstox"},
            execution_venue={"plugin": "sim"},
            fees=FeesConfig(schedule="zerodha_nse"),
            calendars=[{"plugin": "exchange", "exchange": "nse"}],
            secrets={"plugin": "keychain"},
            universe=UniverseConfig(symbols=["NSE:EQ:RELIANCE"]),
            strategies=[
                StrategyConfig(
                    plugin="sma_cross",
                    risk_profile="conservative",
                    params={"timeframe": "1d", "product": "CNC"},
                )
            ],
        )

        report = check_fees_readiness(run_cfg, rt)
        assert report.schedule == "zerodha_nse"
        assert report.has_errors is False
        assert len(report.components) >= 5
        # All required components (brokerage, stt, stamp, exchange_txn, sebi, dp, gst) should be OK
        for comp in report.components:
            assert comp.status == "OK"
            assert comp.earliest_verified <= date(2024, 11, 10)


def test_fees_readiness_unused_components_do_not_block_run(tmp_path: Path) -> None:
    """Zerodha NSE has options exchange_txn starting 2026-10-30; 2024 equity run is unaffected."""
    with open_runtime(root=Path.cwd(), ensure=False) as rt:
        run_cfg = RunConfig(
            allocated_capital=500000,
            run={"name": "test_eq_2024", "mode": "backtest"},
            backtest=BacktestConfig(start="2024-10-01", end="2024-12-01", timeframe="1d"),
            data_source={"plugin": "upstox"},
            execution_venue={"plugin": "sim"},
            fees=FeesConfig(schedule="zerodha_nse"),
            calendars=[{"plugin": "exchange", "exchange": "nse"}],
            secrets={"plugin": "keychain"},
            universe=UniverseConfig(symbols=["NSE:EQ:TCS"]),
            strategies=[
                StrategyConfig(
                    plugin="sma_cross",
                    risk_profile="conservative",
                    params={"timeframe": "1d", "product": "CNC"},
                )
            ],
        )

        report = check_fees_readiness(run_cfg, rt)
        # Should not raise or error for fno:MIS:opt
        assert report.has_errors is False
        # None of the checked components should be for options
        for comp in report.components:
            assert comp.segment != "fno"
            assert comp.status == "OK"


def test_fees_readiness_before_earliest_fails_without_fallback(tmp_path: Path) -> None:
    """Backtest starting before 2020 raises ERROR when fallback unset."""
    with open_runtime(root=Path.cwd(), ensure=False) as rt:
        run_cfg = RunConfig(
            allocated_capital=500000,
            run={"name": "test_old", "mode": "backtest"},
            backtest=BacktestConfig(start="2019-01-01", end="2019-12-31", timeframe="1d"),
            data_source={"plugin": "upstox"},
            execution_venue={"plugin": "sim"},
            fees=FeesConfig(schedule="zerodha_nse"),
            calendars=[{"plugin": "exchange", "exchange": "nse"}],
            secrets={"plugin": "keychain"},
            universe=UniverseConfig(symbols=["NSE:EQ:RELIANCE"]),
            strategies=[
                StrategyConfig(
                    plugin="sma_cross",
                    risk_profile="conservative",
                    params={"timeframe": "1d", "product": "CNC"},
                )
            ],
        )

        report = check_fees_readiness(run_cfg, rt)
        assert report.has_errors is True
        assert "before earliest verified" in report.error_message
        assert "fees.fallback = 'earliest'" in report.remediation_fix
        error_comps = [c for c in report.components if c.status == "ERROR"]
        assert len(error_comps) > 0


def test_fees_readiness_before_earliest_passes_with_fallback(tmp_path: Path) -> None:
    """Backtest starting before 2020 passes with FALLBACK status when fallback='earliest' is set."""
    with open_runtime(root=Path.cwd(), ensure=False) as rt:
        run_cfg = RunConfig(
            allocated_capital=500000,
            run={"name": "test_old_fb", "mode": "backtest"},
            backtest=BacktestConfig(start="2019-01-01", end="2019-12-31", timeframe="1d"),
            data_source={"plugin": "upstox"},
            execution_venue={"plugin": "sim"},
            fees=FeesConfig(schedule="zerodha_nse", fallback="earliest"),
            calendars=[{"plugin": "exchange", "exchange": "nse"}],
            secrets={"plugin": "keychain"},
            universe=UniverseConfig(symbols=["NSE:EQ:RELIANCE"]),
            strategies=[
                StrategyConfig(
                    plugin="sma_cross",
                    risk_profile="conservative",
                    params={"timeframe": "1d", "product": "CNC"},
                )
            ],
        )

        report = check_fees_readiness(run_cfg, rt)
        assert report.has_errors is False
        assert report.fallback_configured is True
        fb_comps = [c for c in report.components if c.status == "FALLBACK"]
        assert len(fb_comps) > 0


def test_fees_readiness_checks_fno_when_present_in_universe(tmp_path: Path) -> None:
    """When FNO instrument is in universe, FNO components are evaluated."""
    with open_runtime(root=Path.cwd(), ensure=False) as rt:
        run_cfg = RunConfig(
            allocated_capital=500000,
            run={"name": "test_fno", "mode": "backtest"},
            backtest=BacktestConfig(start="2024-11-10", end="2025-01-10", timeframe="1d"),
            data_source={"plugin": "upstox"},
            execution_venue={"plugin": "sim"},
            fees=FeesConfig(schedule="zerodha_nse"),
            calendars=[{"plugin": "exchange", "exchange": "nse"}],
            secrets={"plugin": "keychain"},
            universe=UniverseConfig(symbols=["NSE:FNO:NIFTY24DECFUT"]),
            strategies=[
                StrategyConfig(
                    plugin="sma_cross",
                    risk_profile="conservative",
                    params={"timeframe": "1d", "product": "MIS"},
                )
            ],
        )

        report = check_fees_readiness(run_cfg, rt)
        fno_comps = [c for c in report.components if c.segment == "fno"]
        assert len(fno_comps) > 0
        assert all(c.product == "MIS" for c in fno_comps)


def test_sma_1d_bt_does_not_need_fallback() -> None:
    """sma_1d_bt config starts at 2024-11-10 which is >= 2020-01-01 verified date."""
    with open_runtime(root=Path.cwd(), ensure=False) as rt:
        from algodesk.config.snapshot import build_effective

        eff = build_effective(rt.root, run_name="sma_1d_bt")
        assert eff.run is not None
        report = check_fees_readiness(eff.run, rt)
        assert report.has_errors is False
        for c in report.components:
            assert c.status == "OK"


def test_backtest_check_cli_includes_fees_block(tmp_project: Path) -> None:
    """`backtest check` prints Fees Readiness block."""
    run_cfg_path = tmp_project / "config" / "runs" / "test_fee_bt.toml"
    run_cfg_path.write_text(
        """allocated_capital = 100000

[run]
name = "test_fee_bt"
mode = "backtest"

[backtest]
start = "2024-11-10"
end = "2024-11-12"
fidelity = "bar"
timeframe = "1d"
max_missing_pct = 100.0
warmup_bars = 0

[data_source]
plugin = "upstox"

[execution_venue]
plugin = "sim"
seed = 42

[fees]
schedule = "zerodha_nse"

[[calendars]]
plugin = "exchange"
exchange = "nse"

[secrets]
plugin = "keychain"

[universe]
symbols = ["NSE:EQ:RELIANCE"]

[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
params = { timeframe = "1d", fast = 2, slow = 5, qty = 1, product = "CNC" }
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["--root", str(tmp_project), "backtest", "check", "test_fee_bt"])
    assert result.exit_code == 0
    assert "=== Fees Readiness: schedule 'zerodha_nse' ===" in result.stdout
    assert "brokerage" in result.stdout
    assert "stt" in result.stdout


def test_backtest_check_cli_json_mode_includes_fees(tmp_project: Path) -> None:
    """`backtest check --json` includes 'fees' dictionary in JSON output."""
    run_cfg_path = tmp_project / "config" / "runs" / "test_fee_bt.toml"
    run_cfg_path.write_text(
        """allocated_capital = 100000

[run]
name = "test_fee_bt"
mode = "backtest"

[backtest]
start = "2024-11-10"
end = "2024-11-12"
fidelity = "bar"
timeframe = "1d"
max_missing_pct = 100.0
warmup_bars = 0

[data_source]
plugin = "upstox"

[execution_venue]
plugin = "sim"
seed = 42

[fees]
schedule = "zerodha_nse"

[[calendars]]
plugin = "exchange"
exchange = "nse"

[secrets]
plugin = "keychain"

[universe]
symbols = ["NSE:EQ:RELIANCE"]

[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
params = { timeframe = "1d", fast = 2, slow = 5, qty = 1, product = "CNC" }
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["--root", str(tmp_project), "backtest", "check", "test_fee_bt", "--json"]
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert "fees" in data
    assert data["fees"]["schedule"] == "zerodha_nse"
    assert data["fees"]["has_errors"] is False
    assert len(data["fees"]["components"]) > 0
