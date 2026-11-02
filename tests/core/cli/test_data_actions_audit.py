"""Tests for algodesk data actions audit CLI command."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from algodesk.api import Bar, InstrumentId, SeriesKey
from algodesk.api.market import ActionRecord
from algodesk.cli.app import app
from algodesk.data.actions_store import ActionsStore


def _make_bar(
    inst: InstrumentId,
    dt: datetime,
    close: float,
    adj: str = "raw",
    src: str = "yfinance",
) -> Bar:
    return Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=dt,
        end_ts=dt + timedelta(days=1),
        open=close - 5.0,
        high=close + 5.0,
        low=close - 5.0,
        close=close,
        volume=10000,
        complete=True,
    )


def test_actions_audit_matching_series(
    runner: CliRunner,
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    data_dir = tmp_project / "data"
    store = ActionsStore(data_dir=data_dir)
    # 2:1 split on 2024-10-15
    act = ActionRecord(
        ex_date=date(2024, 10, 15),
        type="split",
        ratio=2.0,
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
        status="confirmed",
    )
    store.write("NSE", "RELIANCE", [act])

    # Mock DataService.get to return raw and adjusted bars
    dt1 = datetime(2024, 10, 10, 9, 15, tzinfo=UTC)
    dt2 = datetime(2024, 10, 16, 9, 15, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:RELIANCE")

    # Raw bars: 2000 before split, 1000 after split
    _make_bar(inst, dt1, 2000.0, "raw")
    _make_bar(inst, dt2, 1000.0, "raw")

    # Source-adjusted bars: factor 0.5 before split -> 1000, after split -> 1000
    adj_b1 = _make_bar(inst, dt1, 1000.0, "adjusted")
    adj_b2 = _make_bar(inst, dt2, 1000.0, "adjusted")

    from algodesk.data.service import DataService, DataSlice

    def fake_get(
        self: Any,
        series: Any,
        start: Any = None,
        end: Any = None,
        download: str = "adjusted",
        adjust: str | None = None,
        **kwargs: Any,
    ) -> DataSlice:
        bars = [adj_b1, adj_b2]
        k = SeriesKey(inst, "bars", "1d", download, "yfinance")
        return DataSlice(key=k, start=dt1, end=dt2, bars=tuple(bars))

    monkeypatch.setattr(DataService, "get", fake_get)

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "actions",
            "audit",
            "RELIANCE",
            "--from",
            "2024-10-01",
            "--to",
            "2024-10-20",
        ],
    )
    assert res.exit_code == 0
    assert "Symbol" in res.output
    assert "RELIANCE" in res.output
    assert "OK" in res.output
    assert "Audit completed clean" in res.output


def test_actions_audit_flagged_on_discrepancy(
    runner: CliRunner,
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    data_dir = tmp_project / "data"
    store = ActionsStore(data_dir=data_dir)
    act = ActionRecord(
        ex_date=date(2024, 10, 15),
        type="split",
        ratio=2.0,
        source="yfinance",
        symbol="TCS",
        exchange="NSE",
        status="confirmed",
    )
    store.write("NSE", "TCS", [act])

    dt1 = datetime(2024, 10, 10, 9, 15, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:TCS")

    # Derived adjusted close is 1000.0
    raw_b = _make_bar(inst, dt1, 1000.0, "raw")
    # Source adjusted close is 950.0 (5% difference > 0.5% tolerance)
    adj_b = _make_bar(inst, dt1, 950.0, "adjusted")

    from algodesk.data.service import DataService, DataSlice

    def fake_get(
        self: Any,
        series: Any,
        start: Any = None,
        end: Any = None,
        download: str = "adjusted",
        adjust: str | None = None,
        **kwargs: Any,
    ) -> DataSlice:
        bars = [adj_b] if download == "adjusted" else [raw_b]
        k = SeriesKey(inst, "bars", "1d", download, "yfinance")
        return DataSlice(key=k, start=dt1, end=dt1 + timedelta(days=1), bars=tuple(bars))

    monkeypatch.setattr(DataService, "get", fake_get)

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "actions",
            "audit",
            "TCS",
            "--from",
            "2024-10-01",
            "--to",
            "2024-10-20",
        ],
    )
    assert res.exit_code == 0
    assert "FLAG" in res.output
    assert "TCS" in res.output
    assert "flagged exceeding tolerance" in res.output


def test_actions_audit_no_overlapping_bars(
    runner: CliRunner,
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    from algodesk.data.service import DataService, DataSlice

    k0 = SeriesKey(InstrumentId("NSE:EQ:INFY"), "bars", "1d", "raw", "yfinance")
    t0 = datetime(2024, 10, 1, tzinfo=UTC)
    monkeypatch.setattr(
        DataService,
        "get",
        lambda *args, **kwargs: DataSlice(key=k0, start=t0, end=t0, bars=()),
    )

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "actions",
            "audit",
            "INFY",
            "--from",
            "2024-10-01",
            "--to",
            "2024-10-20",
        ],
    )
    assert res.exit_code == 0
    assert "INFY" in res.output
    assert "NO DATA" in res.output
