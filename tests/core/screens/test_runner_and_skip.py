"""Tests for ScreenRunner, skip reasons, skip guards, look-ahead bias, and result hashes."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from tests.core.screens.conftest import (
    FakeDataReader,
    FakeIndicatorResolver,
    generate_daily_bars,
)

from algodesk.api.errors import CalendarCoverageError
from algodesk.api.market import Bar, InstrumentId
from algodesk.api.protocols import Calendar
from algodesk.api.screens import (
    SkipReason,
)
from algodesk.results.screen_writer import write_screen_results
from algodesk.screens.models import (
    OperandModel,
    RequireRule,
    TomlScreenModel,
)
from algodesk.screens.runner import ScreenRunner

IST = ZoneInfo("Asia/Kolkata")


class MockCalendar(Calendar):
    def __init__(self, start_date: date, end_date: date, holidays: set[date] | None = None) -> None:
        self._start = start_date
        self._end = end_date
        self._holidays = holidays or set()

    def coverage(self) -> tuple[date, date]:
        return self._start, self._end

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        if d < self._start or d > self._end:
            raise CalendarCoverageError(
                what=f"Date {d} outside coverage [{self._start}, {self._end}]",
                where="MockCalendar:is_trading_day",
                fix="Request within coverage",
            )
        if d in self._holidays:
            return False
        return d.weekday() < 5  # Mon-Fri

    def sessions(self, d: date, segment: str = "") -> list[Any]:
        return []

    def next_session(self, ts: datetime, segment: str = "") -> Any:
        return None


def test_skip_reason_no_data(fake_resolver: FakeIndicatorResolver) -> None:
    reader = FakeDataReader()
    # SYM1 not added to reader
    runner = ScreenRunner(reader, fake_resolver, max_skipped_pct=100.0)
    screen = TomlScreenModel(
        name="test_screen",
        require=[
            RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=10.0))
        ],
    )
    result = runner.run(screen, symbols=["SYM1"], as_of=date(2026, 11, 12))
    assert len(result.rows) == 0
    assert len(result.skipped) == 1
    sym, reason, detail = result.skipped[0]
    assert sym == "SYM1"
    assert reason == SkipReason.NO_DATA


def test_skip_reason_insufficient_history(fake_resolver: FakeIndicatorResolver) -> None:
    reader = FakeDataReader()
    # Provide only 5 bars when indicator needs 50 bars
    bars = generate_daily_bars("SYM1", [10.0, 11.0, 12.0, 13.0, 14.0], as_of=date(2026, 11, 12))
    reader.add_bars("SYM1", bars)

    runner = ScreenRunner(reader, fake_resolver, max_skipped_pct=100.0)
    screen = TomlScreenModel(
        name="test_screen",
        require=[
            RequireRule(left=OperandModel(ind="sma", n=50), op=">", right=OperandModel(const=10.0))
        ],
    )
    result = runner.run(screen, symbols=["SYM1"], as_of=date(2026, 11, 12))
    assert len(result.rows) == 0
    assert len(result.skipped) == 1
    sym, reason, detail = result.skipped[0]
    assert sym == "SYM1"
    assert reason == SkipReason.INSUFFICIENT_HISTORY


def test_skip_reason_adjustment_refused(fake_resolver: FakeIndicatorResolver) -> None:
    reader = FakeDataReader()
    reader.add_refusal("SYM1", SkipReason.ADJUSTMENT_REFUSED, "Corporate action demerger conflict")

    runner = ScreenRunner(reader, fake_resolver, max_skipped_pct=100.0)
    screen = TomlScreenModel(
        name="test_screen",
        require=[
            RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=10.0))
        ],
    )
    result = runner.run(screen, symbols=["SYM1"], as_of=date(2026, 11, 12))
    assert len(result.skipped) == 1
    assert result.skipped[0][1] == SkipReason.ADJUSTMENT_REFUSED


def test_skip_reason_calendar_gap(fake_resolver: FakeIndicatorResolver) -> None:
    reader = FakeDataReader()
    reader.add_refusal("SYM1", SkipReason.CALENDAR_GAP, "Missing bars on trading day 2026-11-05")

    runner = ScreenRunner(reader, fake_resolver, max_skipped_pct=100.0)
    screen = TomlScreenModel(
        name="test_screen",
        require=[
            RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=10.0))
        ],
    )
    result = runner.run(screen, symbols=["SYM1"], as_of=date(2026, 11, 12))
    assert len(result.skipped) == 1
    assert result.skipped[0][1] == SkipReason.CALENDAR_GAP


def test_skip_guard_pass_when_below_threshold(fake_resolver: FakeIndicatorResolver) -> None:
    reader = FakeDataReader()
    # 10 symbols: 9 have data, 1 is missing -> 10% skipped <= 10.0% max
    for i in range(1, 10):
        sym = f"SYM{i}"
        bars = generate_daily_bars(sym, [10.0, 20.0, 30.0], as_of=date(2026, 11, 12))
        reader.add_bars(sym, bars)

    runner = ScreenRunner(reader, fake_resolver, max_skipped_pct=10.0)
    screen = TomlScreenModel(
        name="test_screen",
        require=[
            RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=15.0))
        ],
    )
    syms = [f"SYM{i}" for i in range(1, 11)]
    result = runner.run(screen, symbols=syms, as_of=date(2026, 11, 12))
    assert result.verdict == "PASS"
    assert result.fail_reason is None


def test_skip_guard_failure_when_above_threshold_still_writes_folder(
    tmp_path: Path, fake_resolver: FakeIndicatorResolver
) -> None:
    reader = FakeDataReader()
    # 10 symbols: only 5 have data -> 50% skipped > 10.0% max
    for i in range(1, 6):
        sym = f"SYM{i}"
        bars = generate_daily_bars(sym, [10.0, 20.0, 30.0], as_of=date(2026, 11, 12))
        reader.add_bars(sym, bars)

    runner = ScreenRunner(reader, fake_resolver, max_skipped_pct=10.0)
    screen = TomlScreenModel(
        name="test_screen",
        require=[
            RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=15.0))
        ],
    )
    syms = [f"SYM{i}" for i in range(1, 11)]
    result = runner.run(screen, symbols=syms, as_of=date(2026, 11, 12))

    assert result.verdict == "FAIL"
    assert result.fail_reason is not None
    assert "Skipped percentage" in result.fail_reason

    # Verify results folder is written with FAIL verdict
    run_dir = write_screen_results(tmp_path / "results", result)
    assert run_dir.is_dir()
    summary_text = (run_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Verdict: FAIL" in summary_text
    assert (run_dir / "skipped.csv").is_file()
    assert (run_dir / "manifest.json").is_file()


def test_no_look_ahead_bias(fake_resolver: FakeIndicatorResolver) -> None:
    as_of = date(2026, 11, 12)
    sym = "RELIANCE"

    # Base bars up to as_of
    bars_base = generate_daily_bars(sym, [100.0, 105.0, 110.0, 115.0, 120.0], as_of=as_of)
    reader1 = FakeDataReader()
    reader1.add_bars(sym, bars_base)

    runner1 = ScreenRunner(reader1, fake_resolver)
    screen = TomlScreenModel(
        name="trend",
        require=[
            RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=100.0))
        ],
    )
    result1 = runner1.run(screen, symbols=[sym], as_of=as_of)

    # Now create reader2 which has extra future bars strictly AFTER as_of
    bars_future = list(bars_base)
    for extra_day in range(1, 5):
        fut_d = as_of + timedelta(days=extra_day)
        s_dt = datetime.combine(fut_d, time(9, 15), tzinfo=IST).astimezone(UTC)
        e_dt = datetime.combine(fut_d, time(15, 30), tzinfo=IST).astimezone(UTC)
        bars_future.append(
            Bar(
                instrument=InstrumentId(f"NSE:EQ:{sym}"),
                timeframe="1d",
                start_ts=s_dt,
                end_ts=e_dt,
                open=200.0,
                high=205.0,
                low=195.0,
                close=200.0,  # huge future price change
                volume=50000.0,
            )
        )

    reader2 = FakeDataReader()
    reader2.add_bars(sym, bars_future)
    runner2 = ScreenRunner(reader2, fake_resolver)
    result2 = runner2.run(screen, symbols=[sym], as_of=as_of)

    # Results MUST be identical: future bars must be ignored
    assert len(result1.rows) == len(result2.rows) == 1
    assert result1.rows[0].values["close"] == result2.rows[0].values["close"] == 120.0
    assert result1.result_hash == result2.result_hash


def test_stable_result_hash_repeat_run(fake_resolver: FakeIndicatorResolver) -> None:
    sym = "TCS"
    as_of = date(2026, 11, 12)
    bars = generate_daily_bars(sym, [3000.0, 3050.0, 3100.0, 3150.0], as_of=as_of)

    reader = FakeDataReader()
    reader.add_bars(sym, bars)
    runner = ScreenRunner(reader, fake_resolver)

    screen = TomlScreenModel(
        name="sma_screen",
        require=[
            RequireRule(left=OperandModel(ind="sma", n=2), op=">", right=OperandModel(const=3000.0))
        ],
    )

    res1 = runner.run(screen, symbols=[sym], as_of=as_of, screen_file_hash="fixed_hash_123")
    res2 = runner.run(screen, symbols=[sym], as_of=as_of, screen_file_hash="fixed_hash_123")

    assert res1.result_hash == res2.result_hash
    assert len(res1.result_hash) == 64


def test_accept_unverified_flag_recorded(
    tmp_path: Path, fake_resolver: FakeIndicatorResolver
) -> None:
    sym = "INFY"
    as_of = date(2026, 11, 12)
    bars = generate_daily_bars(sym, [1500.0, 1550.0], as_of=as_of)
    reader = FakeDataReader()
    reader.add_bars(sym, bars)

    runner = ScreenRunner(reader, fake_resolver)
    screen = TomlScreenModel(
        name="test_screen",
        require=[
            RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=1000.0))
        ],
    )

    result = runner.run(screen, symbols=[sym], as_of=as_of, accept_unverified=True)
    assert result.flags.get("accept_unverified") is True

    run_dir = write_screen_results(tmp_path / "results", result)
    summary_text = (run_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "accept_unverified=True" in summary_text
    assert "--accept-unverified" in summary_text


def test_date_outside_calendar_coverage_fails_loudly(fake_resolver: FakeIndicatorResolver) -> None:
    cal = MockCalendar(start_date=date(2025, 1, 1), end_date=date(2026, 12, 31))
    reader = FakeDataReader()
    runner = ScreenRunner(reader, fake_resolver, calendar=cal)

    screen = TomlScreenModel(
        name="test_screen",
        require=[
            RequireRule(left=OperandModel(field="close"), op=">", right=OperandModel(const=100.0))
        ],
    )

    # Date in year 2024 is before coverage start (2025-01-01)
    with pytest.raises(CalendarCoverageError) as exc_info:
        runner.run(screen, symbols=["RELIANCE"], as_of=date(2024, 6, 1))
    assert "outside calendar coverage" in exc_info.value.what.lower()
