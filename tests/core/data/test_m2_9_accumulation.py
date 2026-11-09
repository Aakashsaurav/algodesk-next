"""Tests for data accumulation engine and CLI command."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import ConfigError
from algodesk.api.market import SeriesKey
from algodesk.api.protocols import Bar
from algodesk.cli.app import app
from algodesk.data.accumulate import (
    execute_accumulation,
    get_source_retention_days,
    plan_accumulation,
    resolve_completed_sessions,
)


class MockCalendar:
    """Mock trading calendar for testing."""

    def __init__(self, holidays: set[date] | None = None) -> None:
        self.holidays = holidays or set()

    def is_trading_day(self, d: date) -> bool:
        if d.weekday() in (5, 6):
            return False
        return d not in self.holidays


class MockDataService:
    """Mock DataService returning dummy data slices."""

    def __init__(self) -> None:
        self.get_calls: list[dict[str, Any]] = []

    def get(
        self,
        series: SeriesKey,
        start: datetime,
        end: datetime,
        download: str = "raw",
        fill_only: bool = False,
    ) -> Any:
        self.get_calls.append({"series": series, "start": start, "end": end})

        class DummySlice:
            bars = [
                Bar(
                    instrument=series.instrument,
                    timeframe=series.timeframe,
                    start_ts=start,
                    end_ts=start + timedelta(minutes=1),
                    open=100.0,
                    high=105.0,
                    low=99.0,
                    close=102.0,
                    volume=500,
                )
            ]
            gaps: list[Any] = []

        return DummySlice()


def test_resolve_completed_sessions_before_close() -> None:
    cal = MockCalendar()
    # Wednesday at 08:00 UTC (13:30 IST, market still open)
    now_dt = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
    sessions = resolve_completed_sessions(cal, count=3, now_dt=now_dt)
    assert len(sessions) == 3
    # 2026-11-04 is Wednesday, before 15:30 IST, so latest completed is 2026-11-03
    assert sessions[-1] == date(2026, 11, 3)
    assert sessions[-2] == date(2026, 11, 2)
    assert sessions[-3] == date(2026, 10, 30)  # previous Friday


def test_resolve_completed_sessions_after_close() -> None:
    cal = MockCalendar()
    # Wednesday at 16:00 IST (10:30 UTC, market closed)
    now_dt = datetime(2026, 11, 4, 11, 0, tzinfo=UTC)
    sessions = resolve_completed_sessions(cal, count=2, now_dt=now_dt)
    assert len(sessions) == 2
    assert sessions[-1] == date(2026, 11, 4)
    assert sessions[-2] == date(2026, 11, 3)


def test_get_source_retention_days() -> None:
    assert get_source_retention_days("yfinance", "1m") == 7
    assert get_source_retention_days("upstox", "1m") == 1800
    assert get_source_retention_days("yfinance", "1d") == 36500
    assert get_source_retention_days("upstox", "1d") == 36500


def test_plan_accumulation_identifies_fetchable_and_lost(tmp_project: Any) -> None:
    cal = MockCalendar()
    # Mock date far in future to make some sessions older than 7 days
    now_dt = datetime(2026, 11, 20, 16, 0, tzinfo=UTC)
    insts, items = plan_accumulation(
        group="demo5",
        source="yfinance",
        days=10,
        runtime=tmp_project,
        calendar=cal,
        now_dt=now_dt,
    )
    assert len(insts) == 5
    m_1m_items = [it for it in items if it.timeframe == "1m"]
    assert len(m_1m_items) == 5
    # First item should have lost sessions beyond 7 days
    assert len(m_1m_items[0].lost_sessions) > 0


def test_execute_accumulation_fetches_and_reports(tmp_project: Any) -> None:
    cal = MockCalendar()
    now_dt = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)
    ds = MockDataService()

    report = execute_accumulation(
        group="demo5",
        source="yfinance",
        days=3,
        runtime=tmp_project,
        dry_run=False,
        data_service=ds,
        calendar=cal,
        now_dt=now_dt,
    )
    assert report.group == "demo5"
    assert report.rows_added > 0
    assert report.days_still_missing == 0
    assert len(ds.get_calls) > 0


def test_execute_accumulation_dry_run_no_fetch(tmp_project: Any) -> None:
    cal = MockCalendar()
    now_dt = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)
    ds = MockDataService()

    report = execute_accumulation(
        group="demo5",
        source="yfinance",
        days=3,
        runtime=tmp_project,
        dry_run=True,
        data_service=ds,
        calendar=cal,
        now_dt=now_dt,
    )
    assert report.dry_run is True
    assert report.rows_added == 0
    assert len(ds.get_calls) == 0


def test_accumulation_unknown_group_raises(tmp_project: Any) -> None:
    with pytest.raises(ConfigError) as exc:
        plan_accumulation(
            group="non_existent_group_xyz",
            source="yfinance",
            days=5,
            runtime=tmp_project,
        )
    assert "not found" in exc.value.what


def test_cli_data_accumulate_dry_run(tmp_project: Any) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["data", "accumulate", "--group", "demo5", "--dry-run"])
    assert res.exit_code == 0
    assert "Accumulation Plan" in res.stdout
    assert "NSE:EQ:RELIANCE" in res.stdout
