"""Unit tests for 'algodesk indicators' CLI commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from algodesk.cli.app import app

runner = CliRunner()


def test_indicators_list_command() -> None:
    res = runner.invoke(app, ["indicators", "list"])
    assert res.exit_code == 0
    assert "NAME" in res.output
    assert "INPUTS" in res.output
    assert "OUTPUTS" in res.output
    assert "DEFAULTS" in res.output
    assert "WARM-UP" in res.output
    assert "TIMEFRAMES" in res.output

    # Check that all 10 indicators appear
    for name in (
        "sma",
        "ema",
        "rsi",
        "atr",
        "vwap",
        "bollinger",
        "macd",
        "rolling_hl",
        "volume_avg",
        "roc",
    ):
        assert name in res.output


def test_indicators_show_macd() -> None:
    res = runner.invoke(app, ["indicators", "show", "macd"])
    assert res.exit_code == 0
    assert "Indicator:   macd" in res.output
    assert "close" in res.output
    assert "macd, signal, histogram" in res.output
    assert "fast=12" in res.output
    assert "slow=26" in res.output
    assert "signal=9" in res.output
    assert "33 bars" in res.output


def test_indicators_show_vwap() -> None:
    res = runner.invoke(app, ["indicators", "show", "vwap"])
    assert res.exit_code == 0
    assert "Indicator:   vwap" in res.output
    assert "intraday" in res.output


def test_indicators_show_unknown() -> None:
    res = runner.invoke(app, ["indicators", "show", "nonexistent"])
    assert res.exit_code != 0
    assert "not found" in res.output


def test_indicators_compute_sma_reliance() -> None:
    res = runner.invoke(
        app,
        [
            "indicators",
            "compute",
            "sma",
            "RELIANCE",
            "1d",
            "--from",
            "2026-01-01",
            "--to",
            "2026-11-12",
            "--param",
            "n=50",
            "--source",
            "upstox",
        ],
    )
    assert res.exit_code == 0
    assert "DATE" in res.output
    assert "CLOSE" in res.output
    assert "VALUE" in res.output
    # Check that 10 rows are returned
    lines = [line.strip() for line in res.output.strip().split("\n") if line.strip()]
    # At least header + 10 data rows
    assert len(lines) >= 11


def test_indicators_compute_bollinger() -> None:
    res = runner.invoke(
        app,
        [
            "indicators",
            "compute",
            "bollinger",
            "RELIANCE",
            "1d",
            "--from",
            "2026-01-01",
            "--to",
            "2026-11-12",
            "--param",
            "n=20",
            "--param",
            "k=2.0",
            "--source",
            "upstox",
        ],
    )
    assert res.exit_code == 0
    assert "MIDDLE" in res.output
    assert "UPPER" in res.output
    assert "LOWER" in res.output


def test_indicators_compute_macd() -> None:
    res = runner.invoke(
        app,
        [
            "indicators",
            "compute",
            "macd",
            "RELIANCE",
            "1d",
            "--from",
            "2026-01-01",
            "--to",
            "2026-11-12",
            "--source",
            "upstox",
        ],
    )
    assert res.exit_code == 0
    assert "MACD" in res.output
    assert "SIGNAL" in res.output
    assert "HISTOGRAM" in res.output


def test_indicators_compute_missing_data_actionable_error(tmp_project: Path) -> None:
    res = runner.invoke(
        app,
        [
            "indicators",
            "compute",
            "sma",
            "NONEXISTENT_SYMBOL",
            "1d",
            "--from",
            "2026-01-01",
            "--to",
            "2026-01-10",
            "--source",
            "upstox",
        ],
    )
    assert res.exit_code != 0
    assert "algodesk data get" in res.output


def test_indicators_compute_bad_param_format() -> None:
    res = runner.invoke(
        app,
        [
            "indicators",
            "compute",
            "sma",
            "RELIANCE",
            "1d",
            "--from",
            "2026-01-01",
            "--to",
            "2026-11-12",
            "--param",
            "invalid_format_without_equals",
        ],
    )
    assert res.exit_code != 0
