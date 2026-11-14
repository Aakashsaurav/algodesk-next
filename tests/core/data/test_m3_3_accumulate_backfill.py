"""Unit tests for 'algodesk data accumulate' backfill options, chunks, and failure recovery."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from typer.testing import CliRunner

from algodesk.api.market import Bar, SeriesKey
from algodesk.cli.app import app
from algodesk.data.accumulate import (
    execute_accumulation,
    plan_accumulation,
    resolve_completed_sessions,
)


class MockCalendar:
    """Mock trading calendar for test scenarios."""

    def __init__(self, holidays: list[date] | None = None) -> None:
        self.holidays = holidays or []

    def is_trading_day(self, d: date) -> bool:
        if d.weekday() in (5, 6):
            return False
        return d not in self.holidays


class MockBackfillDataService:
    """Mock DataService supporting multi-request chunks and simulated failures."""

    def __init__(self, fail_on_symbol: str | None = None) -> None:
        self.get_calls: list[dict[str, Any]] = []
        self.fail_on_symbol = fail_on_symbol

    def get(
        self,
        series: SeriesKey,
        start: datetime,
        end: datetime,
        download: str = "raw",
        fill_only: bool = False,
        refresh: bool = False,
    ) -> Any:
        if self.fail_on_symbol and self.fail_on_symbol in str(series.instrument):
            raise RuntimeError(f"Simulated network timeout for {series.instrument}")

        self.get_calls.append({"series": series, "start": start, "end": end})

        class DummySlice:
            bars = [
                Bar(
                    instrument=series.instrument,
                    timeframe=series.timeframe,
                    start_ts=start,
                    end_ts=start + timedelta(days=1),
                    open=100.0,
                    high=105.0,
                    low=99.0,
                    close=102.0,
                    volume=5000,
                )
            ]
            gaps: list[Any] = []

        return DummySlice()


def test_resolve_completed_sessions_calendar_days() -> None:
    cal = MockCalendar()
    # Wednesday 2026-11-18 at 16:00 IST
    now_dt = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)
    # 10 calendar days back from 2026-11-18 is 2026-11-08 (Sun)
    # Trading days between 2026-11-08 and 2026-11-18:
    # 2026-11-09(M), 10(Tu), 11(W), 12(Th), 13(F), 16(M), 17(Tu), 18(W) = 8 days
    sessions = resolve_completed_sessions(cal, calendar_days=10, now_dt=now_dt)
    assert len(sessions) == 8
    assert sessions[0] == date(2026, 11, 9)
    assert sessions[-1] == date(2026, 11, 18)


def test_accumulate_accepts_timeframe_1d(tmp_project: Any) -> None:
    cal = MockCalendar()
    now_dt = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)

    insts, items = plan_accumulation(
        group="demo5",
        source="yfinance",
        days=5,
        runtime=tmp_project,
        calendar=cal,
        now_dt=now_dt,
        timeframe="1d",
    )

    assert len(insts) == 5
    assert len(items) == 5  # Exactly 1 item per instrument (no 1m items)
    for it in items:
        assert it.timeframe == "1d"


def test_accumulate_dry_run_request_count(tmp_project: Any) -> None:
    cal = MockCalendar()
    now_dt = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)

    rep = execute_accumulation(
        group="demo5",
        source="yfinance",
        days=5,
        runtime=tmp_project,
        calendar=cal,
        now_dt=now_dt,
        dry_run=True,
        timeframe="1d",
    )

    assert rep.dry_run is True
    # 5 instruments * 1 chunk each = 5 requests
    assert rep.request_count == 5


def test_accumulate_continues_after_failing_symbol_and_lists_failures(
    tmp_project: Any,
) -> None:
    cal = MockCalendar()
    now_dt = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)
    # Fail specifically on RELIANCE
    ds = MockBackfillDataService(fail_on_symbol="RELIANCE")

    rep = execute_accumulation(
        group="demo5",
        source="yfinance",
        days=3,
        runtime=tmp_project,
        data_service=ds,
        calendar=cal,
        now_dt=now_dt,
        timeframe="1d",
    )

    # RELIANCE failed, but the remaining 4 instruments succeeded!
    assert rep.rows_added == 4
    assert len(rep.failures) == 1
    assert "RELIANCE" in rep.failures[0]["instrument"]
    assert "Simulated network timeout" in rep.failures[0]["error"]
    assert rep.days_still_missing > 0


def test_cli_data_accumulate_with_timeframe_and_days(tmp_project: Any) -> None:
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "data",
            "accumulate",
            "--group",
            "demo5",
            "--timeframe",
            "1d",
            "--days",
            "500",
            "--dry-run",
        ],
    )
    assert res.exit_code == 0
    assert "Accumulation Plan" in res.output
    assert "TF" in res.output
    assert "1d" in res.output
    assert "request(s)" in res.output


def test_accumulate_dry_run_json_output(tmp_project: Any) -> None:
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "data",
            "accumulate",
            "--group",
            "demo5",
            "--timeframe",
            "1d",
            "--days",
            "100",
            "--dry-run",
            "--json",
        ],
    )
    assert res.exit_code == 0
    import json

    data = json.loads(res.output)
    assert data["dry_run"] is True
    assert data["group"] == "demo5"
    assert data["timeframe"] == "1d"
    assert data["request_count"] == 5
    assert len(data["plan"]) == 5


def test_accumulate_multi_symbol_request_count_500_days(tmp_project: Any) -> None:
    cal = MockCalendar()
    now_dt = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)

    rep = execute_accumulation(
        group="demo5",
        source="yfinance",
        days=500,
        runtime=tmp_project,
        calendar=cal,
        now_dt=now_dt,
        dry_run=True,
        timeframe="1d",
        calendar_days=True,
    )
    assert rep.dry_run is True
    # 5 instruments * 1 chunk each for 500 days = 5 requests
    assert rep.request_count == 5
    assert len(rep.plan_items) == 5
    for it in rep.plan_items:
        assert it.timeframe == "1d"
        assert it.request_count == 1
        assert len(it.target_sessions) > 300


def test_accumulate_all_failures_reported(tmp_project: Any) -> None:
    cal = MockCalendar()
    now_dt = datetime(2026, 11, 18, 11, 0, tzinfo=UTC)
    # Fail on all symbols
    ds = MockBackfillDataService(fail_on_symbol="NSE:EQ")

    rep = execute_accumulation(
        group="demo5",
        source="yfinance",
        days=3,
        runtime=tmp_project,
        data_service=ds,
        calendar=cal,
        now_dt=now_dt,
        timeframe="1d",
    )
    assert rep.rows_added == 0
    assert len(rep.failures) == 5
    for fail in rep.failures:
        assert "NSE:EQ" in fail["instrument"]
        assert "Simulated network timeout" in fail["error"]
