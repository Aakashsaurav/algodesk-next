"""Edge case and boundary tests for GapPlanner, DataManifest fetch logs, and layout helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from algodesk.api import (
    Bar,
    InstrumentId,
    PluginManifest,
    SeriesKey,
    TradingSession,
)
from algodesk.cli.spike import _check_split_discontinuity
from algodesk.data.layout import bar_file_path, dataset_id_for_key, parse_bar_file_path
from algodesk.data.manifest import DataManifest
from algodesk.data.planner import GapPlanner


class SimpleCalendar:
    def __init__(self, holidays: Sequence[date] = ()) -> None:
        self.holidays = set(holidays)

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def sessions(self, d: date, segment: str = "") -> Sequence[TradingSession]:
        if not self.is_trading_day(d, segment):
            return []
        st = datetime(d.year, d.month, d.day, 9, 15, tzinfo=UTC)
        et = datetime(d.year, d.month, d.day, 15, 30, tzinfo=UTC)
        return [TradingSession(name=f"S_{d.isoformat()}", start=st, end=et)]

    def next_session(self, ts: datetime, segment: str = "") -> TradingSession:
        d = ts.date() + timedelta(days=1)
        st = datetime(d.year, d.month, d.day, 9, 15, tzinfo=UTC)
        et = datetime(d.year, d.month, d.day, 15, 30, tzinfo=UTC)
        return TradingSession(name=f"S_{d.isoformat()}", start=st, end=et)


# -----------------------------------------------------------------------------
# 1. GapPlanner holiday permutations (20 parametrized tests)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("day_idx", list(range(1, 21)))
def test_planner_single_holiday_marking(day_idx: int) -> None:
    hol_date = date(2026, 10, day_idx)
    cal = SimpleCalendar(holidays=[hol_date])
    key = SeriesKey(InstrumentId("NSE:EQ:RELIANCE"), "bars", "1d", "adjusted", "upstox")

    st = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    et = datetime(2026, 10, 20, 15, 30, tzinfo=UTC)

    plan = GapPlanner.plan(key, st, et, calendar=cal)
    if hol_date.weekday() < 5:
        assert hol_date in plan.closed_days
        assert hol_date not in plan.missing_days


# -----------------------------------------------------------------------------
# 2. GapPlanner day spans (10 parametrized tests)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("span_days", [1, 2, 3, 4, 5, 7, 10, 14, 21, 28])
def test_planner_span_durations(span_days: int) -> None:
    cal = SimpleCalendar()
    key = SeriesKey(InstrumentId("NSE:EQ:TCS"), "bars", "1d", "raw", "upstox")
    st = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)
    et = st + timedelta(days=span_days - 1)

    plan = GapPlanner.plan(key, st, et, calendar=cal, chunk_days=30)
    total_days = len(plan.missing_days) + len(plan.closed_days)
    assert total_days == span_days


# -----------------------------------------------------------------------------
# 3. Retention boundary tests (6 cases)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("offset", "expected_status"),
    [
        (-10, "UNAVAILABLE"),
        (-8, "UNAVAILABLE"),
        (-7, "MISSING"),  # Cutoff is 7 days prior; exactly 7 days prior is within window
        (-6, "MISSING"),
        (-5, "MISSING"),
        (0, "MISSING"),
    ],
)
def test_planner_retention_exact_boundaries(offset: int, expected_status: str) -> None:
    cal = SimpleCalendar()
    manifest = PluginManifest(
        name="test_broker",
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="pkg:Broker",
        capabilities={"history": True},
        retention={"1d": 7},
    )
    as_of_dt = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    target_d = as_of_dt.date() + timedelta(days=offset)

    # Force weekday
    if target_d.weekday() >= 5:
        target_d -= timedelta(days=2)

    st = datetime(target_d.year, target_d.month, target_d.day, 9, 15, tzinfo=UTC)
    et = datetime(target_d.year, target_d.month, target_d.day, 15, 30, tzinfo=UTC)
    key = SeriesKey(InstrumentId("NSE:EQ:INFY"), "bars", "1d", "raw", "test_broker")

    plan = GapPlanner.plan(key, st, et, calendar=cal, source_manifest=manifest, as_of=as_of_dt)
    if expected_status == "UNAVAILABLE":
        assert target_d in plan.unavailable_days
    else:
        assert target_d in plan.missing_days


# -----------------------------------------------------------------------------
# 4. DataManifest fetch log recording (10 cases)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("log_idx", list(range(10)))
def test_manifest_fetch_log_recording(tmp_path: Path, log_idx: int) -> None:
    db_file = tmp_path / f"test_manifest_{log_idx}.db"
    m = DataManifest(db_file)

    now_iso = datetime.now(UTC).isoformat()
    m.record_fetch_log(
        chunk=f"chunk_{log_idx}",
        source="upstox",
        started=now_iso,
        finished=now_iso,
        rows=100 + log_idx,
        outcome="OK",
        error="",
    )

    conn = sqlite3.connect(db_file)
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT chunk, source, rows, outcome FROM fetch_log WHERE chunk = ?",
            (f"chunk_{log_idx}",),
        ).fetchone()
        assert row is not None
        assert row[0] == f"chunk_{log_idx}"
        assert row[1] == "upstox"
        assert row[2] == 100 + log_idx
        assert row[3] == "OK"
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# 5. Layout and dataset ID parsing across instruments (10 cases)
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("symbol", "tf", "adj", "source"),
    [
        ("NSE:EQ:RELIANCE", "1d", "adjusted", "upstox"),
        ("NSE:EQ:INFY", "1m", "raw", "yfinance"),
        ("BSE:EQ:TCS", "1d", "adjusted", "yfinance"),
        ("NSE:EQ:HDFCBANK", "1m", "adjusted", "upstox"),
        ("NSE:EQ:SBIN", "1d", "raw", "upstox"),
        ("BSE:EQ:ITC", "1d", "raw", "yfinance"),
        ("NSE:EQ:LT", "1m", "adjusted", "upstox"),
        ("NSE:EQ:WIPRO", "1d", "adjusted", "upstox"),
        ("NSE:EQ:MARUTI", "1m", "raw", "yfinance"),
        ("BSE:EQ:TITAN", "1d", "adjusted", "yfinance"),
    ],
)
def test_layout_dataset_id_roundtrip(symbol: str, tf: str, adj: str, source: str) -> None:
    key = SeriesKey(InstrumentId(symbol), "bars", tf, adj, source)
    ds_id = dataset_id_for_key(key)
    assert symbol in ds_id
    assert tf in ds_id
    assert source in ds_id
    assert tf in ds_id
    p = bar_file_path(Path("/tmp/data"), key, date(2026, 10, 15))
    parsed = parse_bar_file_path(p)
    assert parsed is not None
    assert parsed["source"] == source
    assert parsed["timeframe"] == tf


# -----------------------------------------------------------------------------
# 6. Split discontinuity analysis helper (6 cases)
# -----------------------------------------------------------------------------
def test_split_discontinuity_2_to_1() -> None:
    t_before = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)
    t_after = datetime(2026, 10, 15, 15, 30, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")
    d1 = timedelta(days=1)
    b_before = Bar(inst, "1d", t_before, t_before + d1, 2000, 2010, 1990, 2000, 10, True)
    b_after = Bar(inst, "1d", t_after, t_after + d1, 1000, 1010, 990, 1000, 20, True)
    has_disc, msg = _check_split_discontinuity([b_before, b_after], "2026-10-15")
    assert has_disc is True
    assert "Discontinuity detected" in msg


def test_split_continuity_adjusted_smooth() -> None:
    t_before = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)
    t_after = datetime(2026, 10, 15, 15, 30, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")
    d1 = timedelta(days=1)
    b_before = Bar(inst, "1d", t_before, t_before + d1, 1000, 1010, 990, 1000, 10, True)
    b_after = Bar(inst, "1d", t_after, t_after + d1, 1005, 1015, 995, 1002, 20, True)
    has_disc, msg = _check_split_discontinuity([b_before, b_after], "2026-10-15")
    assert has_disc is False
    assert "Smooth continuous" in msg


def test_split_discontinuity_no_split_date() -> None:
    t1 = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")
    b = Bar(inst, "1d", t1, t1 + timedelta(days=1), 100, 101, 99, 100, 1, True)
    has_disc, msg = _check_split_discontinuity([b], None)
    assert has_disc is False
    assert "Not evaluated" in msg


def test_split_discontinuity_insufficient_bars() -> None:
    t1 = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")
    b = Bar(inst, "1d", t1, t1 + timedelta(days=1), 100, 101, 99, 100, 1, True)
    has_disc, msg = _check_split_discontinuity([b], "2026-10-20")
    assert has_disc is False
    assert "Insufficient bars" in msg


def test_split_discontinuity_invalid_date_format() -> None:
    has_disc, msg = _check_split_discontinuity([], "invalid_format")
    assert has_disc is False


def test_split_discontinuity_reverse_split() -> None:
    t_before = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)
    t_after = datetime(2026, 10, 15, 15, 30, tzinfo=UTC)
    inst = InstrumentId("NSE:EQ:INFY")
    d1 = timedelta(days=1)
    b_before = Bar(inst, "1d", t_before, t_before + d1, 100, 105, 95, 100, 10, True)
    b_after = Bar(inst, "1d", t_after, t_after + d1, 200, 205, 195, 200, 20, True)
    has_disc, msg = _check_split_discontinuity([b_before, b_after], "2026-10-15")
    assert has_disc is True
    assert "Discontinuity detected" in msg
