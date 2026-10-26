"""Tests for algodesk fees CLI commands."""

from __future__ import annotations

from typer.testing import CliRunner

from algodesk.cli.app import app


def test_fees_calc_accept_trade(runner: CliRunner) -> None:
    res = runner.invoke(
        app,
        [
            "fees",
            "calc",
            "--exchange",
            "nse",
            "--segment",
            "eq",
            "--product",
            "MIS",
            "--side",
            "sell",
            "--qty",
            "10",
            "--price",
            "2500",
            "--date",
            "2026-10-26",
        ],
    )
    assert res.exit_code == 0
    assert "brokerage" in res.output
    assert "7.50" in res.output
    assert "stt" in res.output
    assert "6.25" in res.output
    assert "16.01" in res.output


def test_fees_calc_delivery_buy(runner: CliRunner) -> None:
    res = runner.invoke(
        app,
        [
            "fees",
            "calc",
            "--exchange",
            "nse",
            "--segment",
            "eq",
            "--product",
            "CNC",
            "--side",
            "buy",
            "--qty",
            "100",
            "--price",
            "500",
            "--date",
            "2026-10-26",
        ],
    )
    assert res.exit_code == 0
    assert "59.32" in res.output


def test_fees_calc_invalid_segment(runner: CliRunner) -> None:
    res = runner.invoke(
        app,
        [
            "fees",
            "calc",
            "--exchange",
            "nse",
            "--segment",
            "crypto",
            "--product",
            "MIS",
            "--side",
            "buy",
            "--qty",
            "10",
            "--price",
            "100",
            "--date",
            "2026-10-26",
        ],
    )
    assert res.exit_code == 1
    assert "ERROR:" in res.output


def test_fees_vectors(runner: CliRunner) -> None:
    res = runner.invoke(app, ["fees", "vectors"])
    assert res.exit_code == 0
    assert (
        "Computed fee vectors (pending your hand check against Zerodha's calculator):" in res.output
    )
    assert "computed" in res.output
    assert "59.32" in res.output
    assert "67.75" in res.output
    assert "41.84" in res.output
    assert "16.01" in res.output
    assert "134.40" in res.output
