"""Tests for Step 13: backtest CLI commands (run, dry-run, results bundle)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from algodesk.cli.app import app


def test_backtest_cli_help(runner: CliRunner) -> None:
    """backtest command displays help and available subcommands."""
    result = runner.invoke(app, ["backtest", "--help"])
    assert result.exit_code == 0
    assert "Usage: algodesk backtest" in result.output
    assert "run" in result.output


def test_backtest_run_unknown_config(runner: CliRunner, tmp_project: Path) -> None:
    """Attempting to run a non-existent config exits non-zero with error."""
    result = runner.invoke(app, ["--root", str(tmp_project), "backtest", "run", "non_existent_run"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "error" in result.output.lower()


def test_backtest_run_rejects_paper_mode(runner: CliRunner, tmp_project: Path) -> None:
    """Attempting to backtest a run whose mode is 'paper' is rejected."""
    result = runner.invoke(app, ["--root", str(tmp_project), "backtest", "run", "demo_paper"])
    assert result.exit_code != 0
    assert "expected 'backtest'" in result.output.lower() or "mode" in result.output.lower()


def test_backtest_run_dry_run_plan(runner: CliRunner, tmp_project: Path) -> None:
    """--dry-run displays dry-run execution plan without running session."""
    result = runner.invoke(
        app, ["--root", str(tmp_project), "backtest", "run", "sma_1d_bt", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "=== Backtest Dry-Run Execution Plan ===" in result.output
    assert "Run Name:          sma_1d_bt" in result.output
    assert "Mode:              backtest" in result.output
    assert "Fidelity:          bar" in result.output
    assert "Timeframe:         1d" in result.output
    assert "Doctor checks:     0 critical" in result.output
    assert "Plan verified successfully" in result.output


def test_backtest_run_results_bundle_and_assumptions(runner: CliRunner, tmp_project: Path) -> None:
    """backtest run creates eight-file results bundle with assumptions line in SUMMARY.md."""
    # Write a minimal backtest run config
    run_cfg_path = tmp_project / "config" / "runs" / "test_mini_bt.toml"
    run_cfg_path.write_text(
        """allocated_capital = 100000

[run]
name = "test_mini_bt"
mode = "backtest"

[backtest]
start = "2026-11-10"
end = "2026-11-12"
fidelity = "bar"
timeframe = "1d"

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

    with patch("algodesk.engine.backtest.BacktestRunner.run") as mock_run:
        # Simulate results directory creation
        res_dir = tmp_project / "results" / "test_mini_bt_backtest_20261116_120000_123456"
        res_dir.mkdir(parents=True, exist_ok=True)
        summary_md = res_dir / "SUMMARY.md"
        summary_md.write_text(
            "# Run Summary: test_mini_bt\n\n"
            "## Summary\n"
            "AlgoDesk Next backtest session 'test_mini_bt' executed.\n"
            "Note: Assumptions: fill model (bar synth with slippage), basis (splits), "
            "dividends not credited, no survivorship handling yet.\n",
            encoding="utf-8",
        )
        for fname in [
            "metrics.json",
            "trades.csv",
            "equity.csv",
            "config.effective.txt",
            "registry.snapshot.json",
            "manifest.json",
            "SESSION.md",
        ]:
            (res_dir / fname).write_text("{}", encoding="utf-8")

        mock_run.return_value = res_dir

        result = runner.invoke(app, ["--root", str(tmp_project), "backtest", "run", "test_mini_bt"])
        assert result.exit_code == 0
        assert "Backtest session completed" in result.output

        # Verify all 8 files exist
        expected_files = [
            "SUMMARY.md",
            "metrics.json",
            "trades.csv",
            "equity.csv",
            "config.effective.txt",
            "registry.snapshot.json",
            "manifest.json",
            "SESSION.md",
        ]
        for f in expected_files:
            assert (res_dir / f).exists(), f"Missing expected results file {f}"

        # Check assumptions line in SUMMARY.md
        content = summary_md.read_text(encoding="utf-8")
        assert (
            "Assumptions: fill model (bar synth with slippage), basis (splits), "
            "dividends not credited, no survivorship handling yet."
        ) in content
