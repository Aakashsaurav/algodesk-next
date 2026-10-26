"""Tests for algodesk data CLI commands."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from algodesk.api import Bar, InstrumentId
from algodesk.cli.app import app

runner = CliRunner()


def test_data_get_command_mocked() -> None:
    """Test data get formats table without network calls."""
    fake_bar = Bar(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        timeframe="1d",
        start_ts=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        end_ts=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        open=100.0,
        high=105.0,
        low=99.0,
        close=103.0,
        volume=1000,
        complete=True,
    )

    mock_broker = MagicMock()
    mock_broker.history.return_value = [fake_bar]

    with patch("algodesk.registry.registry.PluginRegistry.create", return_value=mock_broker):
        res = runner.invoke(app, ["data", "get", "RELIANCE", "1d", "2026-10-01", "2026-10-02"])
        assert res.exit_code == 0, res.output
        assert "DATE" in res.output
        assert "100.00" in res.output
        assert "103.00" in res.output
        assert "1000" in res.output


def test_data_get_empty_results() -> None:
    """Test data get handles empty bar results gracefully."""
    mock_broker = MagicMock()
    mock_broker.history.return_value = []

    with patch("algodesk.registry.registry.PluginRegistry.create", return_value=mock_broker):
        res = runner.invoke(app, ["data", "get", "RELIANCE", "1d", "2026-10-01", "2026-10-02"])
        assert res.exit_code == 0, res.output
        assert "No bars returned" in res.output
