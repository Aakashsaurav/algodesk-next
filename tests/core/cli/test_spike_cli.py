"""Tests for algodesk spike CLI commands."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from algodesk.api import Bar, SeriesKey
from algodesk.cli.app import app
from algodesk.registry.registry import PluginRegistry


class FakeUpstoxBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> list[Bar]:
        self.calls.append((key, start, end))
        # Return 2 daily bars
        t1 = datetime(2026, 10, 10, 9, 15, tzinfo=UTC)
        t2 = datetime(2026, 10, 16, 9, 15, tzinfo=UTC)
        b1 = Bar(
            instrument=key.instrument,
            timeframe=key.timeframe,
            start_ts=t1,
            end_ts=t1 + timedelta(days=1),
            open=2800.0,
            high=2850.0,
            low=2790.0,
            close=2820.0,
            volume=500000,
            complete=True,
        )
        b2 = Bar(
            instrument=key.instrument,
            timeframe=key.timeframe,
            start_ts=t2,
            end_ts=t2 + timedelta(days=1),
            open=1410.0,
            high=1430.0,
            low=1400.0,
            close=1415.0,
            volume=800000,
            complete=True,
        )
        return [b1, b2]

    def actions(self, symbol: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "fields": ["date", "type", "ratio"],
            "events": [{"date": "2026-10-15", "type": "split", "ratio": "2:1"}],
        }


class FakeYFinanceBroker:
    def history(self, key: SeriesKey, start: datetime, end: datetime) -> list[Bar]:
        t1 = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
        b1 = Bar(
            instrument=key.instrument,
            timeframe=key.timeframe,
            start_ts=t1,
            end_ts=t1 + timedelta(days=1),
            open=2500.0,
            high=2550.0,
            low=2490.0,
            close=2520.0,
            volume=1000000,
            complete=True,
        )
        return [b1]


def test_spike_upstox_history(
    runner: CliRunner,
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    orig_create = PluginRegistry.create

    def fake_create(self: Any, kind: str, name: str, **kwargs: Any) -> Any:
        if kind == "broker" and name == "upstox":
            return FakeUpstoxBroker()
        return orig_create(self, kind, name, **kwargs)

    monkeypatch.setattr(PluginRegistry, "create", fake_create)

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "spike",
            "upstox-history",
            "--symbol",
            "RELIANCE",
            "--split-date",
            "2026-10-15",
        ],
    )
    assert res.exit_code == 0
    assert "Spike Upstox History: RELIANCE" in res.output
    assert "Discontinuity detected" in res.output
    assert "my_secret_token" not in res.output

    report_file = tmp_project / "_workbench" / "spike_upstox-history.md"
    assert report_file.exists()
    content = report_file.read_text()
    assert "## Summary" in content
    assert "RELIANCE" in content
    assert "token" not in content.lower() or "not disclosing tokens" in content


def test_spike_yfinance_history(
    runner: CliRunner,
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    orig_create = PluginRegistry.create

    def fake_create(self: Any, kind: str, name: str, **kwargs: Any) -> Any:
        if kind == "broker" and name == "yfinance":
            return FakeYFinanceBroker()
        return orig_create(self, kind, name, **kwargs)

    monkeypatch.setattr(PluginRegistry, "create", fake_create)

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "spike",
            "yfinance-history",
            "--symbol",
            "INFY",
        ],
    )
    assert res.exit_code == 0
    assert "Spike Yfinance History: INFY" in res.output

    report_file = tmp_project / "_workbench" / "spike_yfinance-history.md"
    assert report_file.exists()
    content = report_file.read_text()
    assert "## Summary" in content


def test_spike_upstox_actions(
    runner: CliRunner,
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    orig_create = PluginRegistry.create

    def fake_create(self: Any, kind: str, name: str, **kwargs: Any) -> Any:
        if kind == "broker" and name == "upstox":
            return FakeUpstoxBroker()
        return orig_create(self, kind, name, **kwargs)

    monkeypatch.setattr(PluginRegistry, "create", fake_create)

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "spike",
            "upstox-actions",
            "--symbols",
            "RELIANCE,TCS",
        ],
    )
    assert res.exit_code == 0
    assert "Upstox Corporate Actions Spike" in res.output
    assert "RELIANCE" in res.output

    report_file = tmp_project / "_workbench" / "spike_upstox-actions.md"
    assert report_file.exists()
    content = report_file.read_text()
    assert "## Summary" in content
    assert "RELIANCE" in content


def test_spike_compare_adjustment(
    runner: CliRunner,
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    class CompareUpstoxBroker(FakeUpstoxBroker):
        def history(self, key: SeriesKey, start: datetime, end: datetime) -> list[Bar]:
            t1 = datetime(2024, 10, 25, 9, 15, tzinfo=UTC)
            t2 = datetime(2024, 10, 28, 9, 15, tzinfo=UTC)
            return [
                Bar(
                    key.instrument,
                    key.timeframe,
                    t1,
                    t1 + timedelta(days=1),
                    1350.0,
                    1360.0,
                    1340.0,
                    1355.0,
                    1000,
                    True,
                ),
                Bar(
                    key.instrument,
                    key.timeframe,
                    t2,
                    t2 + timedelta(days=1),
                    1360.0,
                    1370.0,
                    1350.0,
                    1365.0,
                    1200,
                    True,
                ),
            ]

    class CompareYFinanceBroker(FakeYFinanceBroker):
        def history(self, key: SeriesKey, start: datetime, end: datetime) -> list[Bar]:
            t1 = datetime(2024, 10, 25, 9, 15, tzinfo=UTC)
            t2 = datetime(2024, 10, 28, 9, 15, tzinfo=UTC)
            # If raw, close before bonus is 2710.0; if adjusted, close is 1355.0
            p1 = 1355.0 if key.adjustment == "adjusted" else 2710.0
            p2 = 1365.0
            return [
                Bar(
                    key.instrument,
                    key.timeframe,
                    t1,
                    t1 + timedelta(days=1),
                    p1,
                    p1 + 10.0,
                    p1 - 10.0,
                    p1,
                    1000,
                    True,
                ),
                Bar(
                    key.instrument,
                    key.timeframe,
                    t2,
                    t2 + timedelta(days=1),
                    p2,
                    p2 + 10.0,
                    p2 - 10.0,
                    p2,
                    1200,
                    True,
                ),
            ]

        def actions(self, symbol: str) -> list[Any]:
            from datetime import date

            from algodesk.api.market import ActionRecord

            return [
                ActionRecord(
                    ex_date=date(2024, 10, 28),
                    type="bonus",
                    ratio=2.0,
                    source="yfinance",
                    symbol=symbol,
                    exchange="NSE",
                )
            ]

    orig_create = PluginRegistry.create

    def fake_create(self: Any, kind: str, name: str, **kwargs: Any) -> Any:
        if kind == "broker" and name == "upstox":
            return CompareUpstoxBroker()
        if kind == "broker" and name == "yfinance":
            return CompareYFinanceBroker()
        return orig_create(self, kind, name, **kwargs)

    monkeypatch.setattr(PluginRegistry, "create", fake_create)

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "spike",
            "compare-adjustment",
            "--symbol",
            "RELIANCE",
            "--from",
            "2024-10-01",
            "--to",
            "2024-11-01",
        ],
    )
    assert res.exit_code == 0
    assert "Adjustment Basis Comparison" in res.output
    assert "RELIANCE" in res.output
    assert "Detected Basis" in res.output
    assert "token" not in res.output.lower() or "not disclosing tokens" in res.output

    report_file = tmp_project / "_workbench" / "spike_compare-adjustment.md"
    assert report_file.exists()
    content = report_file.read_text()
    assert "## Summary" in content
    assert "RELIANCE" in content
    assert "Detected Upstox Candle Basis" in content
