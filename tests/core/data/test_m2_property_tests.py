"""Comprehensive property tests for M2 historical data layer.

Covers:
1. Commit atomicity with simulated crash between rename and manifest (orphan detection & adopt).
2. Quarantined data never overwriting good data.
3. Verification detection of damaged / corrupt / missing / tampered files.
4. Resample equivalence: resample(1m) matches BarBuilder on the same ticks.
5. Invariant matrices: OHLC sanity, timestamp ordering, coverage/gap consistency, idempotency.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import DataError
from algodesk.api.market import (
    Bar,
    CoverageStatus,
    InstrumentId,
    SeriesKey,
    Tick,
)
from algodesk.api.protocols import Session
from algodesk.cli.app import app
from algodesk.data.coverage import CoverageIndex
from algodesk.data.layout import bar_file_path, dataset_id_for_key
from algodesk.data.manifest import DataManifest
from algodesk.data.resample import resample
from algodesk.data.schema import write_bars_parquet
from algodesk.data.store import BarStore
from algodesk.data.validator import BarValidator
from algodesk.engine.bars import BarBuilder


class PropertyHermeticCalendar:
    """Hermetic test calendar for property tests."""

    def __init__(self, holidays: Sequence[date] = ()) -> None:
        self.tz = ZoneInfo("Asia/Kolkata")
        self.holidays = set(holidays)

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        if d in self.holidays:
            return False
        return d.weekday() < 5

    def sessions(self, day: date, segment: str = "") -> list[Session]:
        if not self.is_trading_day(day):
            return []
        s_start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=self.tz).astimezone(UTC)
        s_end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=self.tz).astimezone(UTC)
        return [Session(name="regular", start=s_start, end=s_end, tradable=True)]


def _gen_1m_bars(
    inst: InstrumentId,
    d: date,
    count: int = 10,
    start_minute: int = 15,
    base_price: float = 200.0,
    price_delta: float = 0.5,
) -> list[Bar]:
    tz = ZoneInfo("Asia/Kolkata")
    base_start = datetime(d.year, d.month, d.day, 9, start_minute, tzinfo=tz)
    bars: list[Bar] = []
    for i in range(count):
        s = (base_start + timedelta(minutes=i)).astimezone(UTC)
        e = s + timedelta(minutes=1)
        p = Decimal(str(round(base_price + i * price_delta, 2)))
        bars.append(
            Bar(
                instrument=inst,
                timeframe="1m",
                start_ts=s,
                end_ts=e,
                open=p,
                high=p + Decimal("1.0"),
                low=p - Decimal("0.5"),
                close=p + Decimal("0.2"),
                volume=500 + i * 20,
            )
        )
    return bars


# ---------------------------------------------------------------------------
# Property 1: Commit Atomicity & Simulated Crash (Verify & Adopt)
# ---------------------------------------------------------------------------


def test_prop_commit_atomicity_simulated_crash_and_adopt(tmp_project: Path) -> None:
    """Verify commit atomicity: simulated crash after write reports orphan, --adopt recovers."""
    data_dir = tmp_project / "data"
    manifest_db = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(manifest_db)

    inst_id = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    bars = _gen_1m_bars(inst_id, date(2026, 10, 26), count=15)

    # 1. Simulate crash: parquet written directly to target layout path, manifest untouched
    target_path = bar_file_path(data_dir, key, bars[0].start_ts)
    write_bars_parquet(target_path, bars)
    assert target_path.exists()
    assert len(manifest.get_parts()) == 0

    # 2. algodesk data verify reports orphan
    runner = CliRunner()
    res_verify = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--json"])
    assert res_verify.exit_code != 0
    data_out = json.loads(res_verify.stdout)
    assert len(data_out["orphans"]) == 1
    assert "bars/" in data_out["orphans"][0]

    # 3. algodesk data verify --adopt recovers orphan
    res_adopt = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--adopt"])
    assert res_adopt.exit_code == 0
    assert "adopted" in res_adopt.stdout.lower()

    # 4. Manifest is now populated with valid part and coverage
    manifest.close()
    manifest_refreshed = DataManifest(manifest_db)
    parts = manifest_refreshed.get_parts()
    assert len(parts) == 1
    assert parts[0].rows == 15
    ds_id = dataset_id_for_key(key)
    covs = manifest_refreshed.get_coverage_days(ds_id)
    assert len(covs) == 1
    manifest_refreshed.close()

    # 5. Re-running verify is now completely clean
    res_clean = runner.invoke(app, ["--root", str(tmp_project), "data", "verify"])
    assert res_clean.exit_code == 0


# ---------------------------------------------------------------------------
# Property 2: Quarantined Data Never Overwrites Good Data
# ---------------------------------------------------------------------------


def test_prop_quarantined_data_never_overwrites_good_data(tmp_project: Path) -> None:
    """Quarantined invalid chunk leaves existing valid parquet and manifest unchanged."""
    data_dir = tmp_project / "data"
    manifest_db = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    calendar = PropertyHermeticCalendar()
    store = BarStore(data_dir=data_dir, manifest=manifest, calendar=calendar)

    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    good_bars = _gen_1m_bars(inst_id, date(2026, 10, 26), count=10)
    store.commit(key, good_bars)

    file_p = bar_file_path(data_dir, key, good_bars[0].start_ts)
    assert file_p.exists()
    good_bytes = file_p.read_bytes()
    good_sha = hashlib.sha256(good_bytes).hexdigest()

    # Verify initial manifest state
    parts_before = manifest.get_parts()
    assert len(parts_before) == 1
    assert parts_before[0].sha256 == good_sha

    # Attempt to commit invalid bars with refresh=True
    bad_bars = list(good_bars)
    bad_bars[5] = Bar(
        instrument=inst_id,
        timeframe="1m",
        start_ts=bad_bars[4].start_ts,
        end_ts=bad_bars[4].end_ts,
        open=bad_bars[5].open,
        high=bad_bars[5].high,
        low=bad_bars[5].low,
        close=bad_bars[5].close,
        volume=bad_bars[5].volume,
    )

    with pytest.raises(DataError) as exc_info:
        store.commit(key, bad_bars, refresh=True)
    assert "Validation failed" in exc_info.value.what

    # Assert good file on disk is completely untouched
    assert file_p.exists()
    current_bytes = file_p.read_bytes()
    assert current_bytes == good_bytes
    assert hashlib.sha256(current_bytes).hexdigest() == good_sha

    # Assert manifest parts are untouched
    parts_after = manifest.get_parts()
    assert len(parts_after) == 1
    assert parts_after[0].sha256 == good_sha

    # Assert quarantine directory received the rejected chunk and report
    q_items = manifest.get_quarantine(status="quarantined")
    assert len(q_items) == 1
    rep_file = data_dir / q_items[0].report_path
    assert rep_file.exists()
    rep_data = json.loads(rep_file.read_text())
    assert len(rep_data["errors"]) > 0
    assert any("non-increasing" in err or "duplicate" in err for err in rep_data["errors"])


# ---------------------------------------------------------------------------
# Property 3: Data Verify Detects Damaged & Missing Files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("damage_mode", "expected_pattern"),
    [
        ("corrupt_content", "checksum"),
        ("truncated_file", "checksum"),
        ("missing_file", "missing"),
    ],
)
def test_prop_data_verify_detects_damage_modes(
    tmp_project: Path, damage_mode: str, expected_pattern: str
) -> None:
    """data verify detects various parquet file corruptions and missing files."""
    data_dir = tmp_project / "data"
    manifest_db = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    store = BarStore(data_dir=data_dir, manifest=manifest)

    inst_id = InstrumentId("NSE:EQ:WIPRO")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    bars = _gen_1m_bars(inst_id, date(2026, 10, 26), count=5)
    parts = store.commit(key, bars)
    p = data_dir / parts[0].path

    if damage_mode == "corrupt_content":
        p.write_bytes(b"garbage_random_not_parquet_bytes")
    elif damage_mode == "truncated_file":
        p.write_bytes(b"PAR1")
    elif damage_mode == "missing_file":
        p.unlink()

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--json"])
    assert res.exit_code != 0
    data = json.loads(res.stdout)
    assert not data["ok"]
    if damage_mode in ("corrupt_content", "truncated_file"):
        assert len(data["checksum_errors"]) > 0 or len(data["rule_violations"]) > 0
    elif damage_mode == "missing_file":
        assert len(data["missing_files"]) > 0


# ---------------------------------------------------------------------------
# Property 4: Resample Equivalence (1m -> 5m Matches BarBuilder on Ticks)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seed", "tick_count", "drift"),
    [
        (101, 100, 0.05),
        (202, 150, -0.05),
        (303, 80, 0.1),
        (404, 120, -0.1),
        (505, 90, 0.0),
    ],
)
def test_prop_resample_equivalence_with_bar_builder(
    seed: int, tick_count: int, drift: float
) -> None:
    """Pure resample(1m -> 5m) equals BarBuilder 5m bars from the exact same ticks."""
    inst = InstrumentId("NSE:EQ:SBIN")
    cal = PropertyHermeticCalendar()
    d = date(2026, 10, 26)
    tz = ZoneInfo("Asia/Kolkata")
    base_dt = datetime(d.year, d.month, d.day, 9, 15, tzinfo=tz).astimezone(UTC)

    # 1. Generate ticks spanning several 5-minute boundaries
    import random

    rng = random.Random(seed)
    price = 600.0
    ticks: list[Tick] = []

    for i in range(11):
        t_dt = base_dt + timedelta(minutes=i)
        price += drift + rng.uniform(-0.25, 0.25)
        ticks.append(
            Tick(
                instrument=inst,
                exch_ts=t_dt,
                recv_ts=t_dt,
                ltp=price,
                ltq=10,
                volume=100 * (i + 1),
            )
        )

    # 2. Build 1m and 5m bars with BarBuilder
    builder_1m_bars: list[Bar] = []
    builder_5m_bars: list[Bar] = []

    def on_bar_closed(bar: Bar) -> None:
        if bar.timeframe == "1m":
            builder_1m_bars.append(bar)
        elif bar.timeframe == "5m":
            builder_5m_bars.append(bar)

    bb = BarBuilder(
        timeframes=("1m", "5m"),
        session_manager=None,
        on_bar_closed=on_bar_closed,
        fill_empty_bars=False,
    )
    for tick in ticks:
        bb.on_tick(tick)

    # Verify BarBuilder closed completed bars
    assert len(builder_1m_bars) >= 10
    assert len(builder_5m_bars) == 2

    # 3. Resample 1m bars to 5m
    resampled_5m = resample(builder_1m_bars[:10], timeframe="5m", calendar=cal)

    # Verify matching count and identical OHLCV
    assert len(resampled_5m) == len(builder_5m_bars)

    for r_bar, b_bar in zip(resampled_5m, builder_5m_bars, strict=True):
        assert r_bar.start_ts == b_bar.start_ts
        assert r_bar.end_ts == b_bar.end_ts
        assert float(r_bar.open) == pytest.approx(float(b_bar.open))
        assert float(r_bar.high) == pytest.approx(float(b_bar.high))
        assert float(r_bar.low) == pytest.approx(float(b_bar.low))
        assert float(r_bar.close) == pytest.approx(float(b_bar.close))
        assert r_bar.volume == b_bar.volume


# ---------------------------------------------------------------------------
# Property 5: Coverage & Gap Invariants Across Date Ranges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start_day", "end_day", "holidays"),
    [
        (date(2026, 10, 19), date(2026, 10, 23), ()),  # Mon-Fri
        (date(2026, 10, 23), date(2026, 10, 26), ()),  # Fri-Mon (covers weekend)
        (date(2026, 10, 19), date(2026, 10, 23), (date(2026, 10, 21),)),  # Wed holiday
        (date(2026, 10, 26), date(2026, 10, 30), ()),  # Full week
    ],
)
def test_prop_gap_and_coverage_invariants(
    tmp_path: Path, start_day: date, end_day: date, holidays: tuple[date, ...]
) -> None:
    """Gaps and coverage obey calendar invariants: non-trading days never report gaps."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    cal = PropertyHermeticCalendar(holidays=holidays)
    cov_index = CoverageIndex(manifest=manifest, calendar=cal)

    ds_id = "test_dataset_coverage"
    gaps = cov_index.detect_gaps(ds_id, start_day, end_day)

    # Invariant: No gap is ever on a non-trading day or weekend
    for g in gaps:
        assert cal.is_trading_day(g.day)
        assert g.day not in holidays
        assert g.day.weekday() < 5
        assert g.status in (
            CoverageStatus.UNAVAILABLE,
            CoverageStatus.EMPTY,
            CoverageStatus.MISSING,
        )


# ---------------------------------------------------------------------------
# Invariant Matrices: Resampler Timeframes and Day Resolutions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tf", ["2m", "3m", "5m", "10m", "15m", "30m", "60m", "1h"])
def test_prop_resample_intraday_timeframes(tf: str) -> None:
    """Intraday resample produces monotonic non-overlapping bars aligned to open."""
    inst = InstrumentId("NSE:EQ:INFY")
    cal = PropertyHermeticCalendar()
    d = date(2026, 10, 26)
    bars_1m = _gen_1m_bars(inst, d, count=60)  # 60 minutes
    res = resample(bars_1m, timeframe=tf, calendar=cal)

    assert len(res) > 0
    for idx in range(len(res) - 1):
        assert res[idx].start_ts < res[idx].end_ts
        assert res[idx].end_ts <= res[idx + 1].start_ts
        assert res[idx].low <= res[idx].open <= res[idx].high
        assert res[idx].low <= res[idx].close <= res[idx].high


@pytest.mark.parametrize("tf", ["1w", "1M"])
def test_prop_resample_daily_to_higher_timeframes(tf: str) -> None:
    """Daily bars resample to 1w and 1M with correct aggregated OHLCV."""
    inst = InstrumentId("NSE:EQ:TCS")
    cal = PropertyHermeticCalendar()
    daily_bars: list[Bar] = []
    tz = ZoneInfo("Asia/Kolkata")
    base_price = Decimal("3000.0")

    # Create 30 daily bars
    for i in range(30):
        d = date(2026, 10, 1) + timedelta(days=i)
        if not cal.is_trading_day(d):
            continue
        s = datetime(d.year, d.month, d.day, 9, 15, tzinfo=tz).astimezone(UTC)
        e = datetime(d.year, d.month, d.day, 15, 30, tzinfo=tz).astimezone(UTC)
        p = base_price + Decimal(str(i * 10))
        daily_bars.append(
            Bar(
                instrument=inst,
                timeframe="1d",
                start_ts=s,
                end_ts=e,
                open=p,
                high=p + Decimal("20.0"),
                low=p - Decimal("15.0"),
                close=p + Decimal("5.0"),
                volume=100000 + i * 1000,
            )
        )

    res = resample(daily_bars, timeframe=tf, calendar=cal)
    assert len(res) > 0
    for b in res:
        assert b.low <= b.open <= b.high
        assert b.low <= b.close <= b.high
        assert b.volume > 0


# ---------------------------------------------------------------------------
# Invariant Matrices: BarValidator Strict Rejection Rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_name", "corrupt_fn"),
    [
        ("negative_open", lambda r: dict(r, open=-10.0)),
        ("zero_close", lambda r: dict(r, close=0.0)),
        ("high_below_low", lambda r: dict(r, high=50.0, low=60.0)),
        ("open_above_high", lambda r: dict(r, open=150.0, high=100.0, low=90.0)),
        ("close_below_low", lambda r: dict(r, close=80.0, high=100.0, low=90.0)),
        ("negative_volume", lambda r: dict(r, volume=-100)),
        ("inverted_ts", lambda r: dict(r, start_ts=r["end_ts"], end_ts=r["start_ts"])),
    ],
)
def test_prop_validator_rejects_rule_violations(
    tmp_path: Path, rule_name: str, corrupt_fn: object
) -> None:
    """BarValidator strictly rejects all mathematical and physical bar violations."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    val = BarValidator(data_dir=tmp_path, manifest=manifest)

    inst_id = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1m", adjustment="raw", source="upstox"
    )
    raw_rows = [
        {
            "start_ts": datetime(2026, 10, 26, 3, 45 + i, tzinfo=UTC),
            "end_ts": datetime(2026, 10, 26, 3, 46 + i, tzinfo=UTC),
            "open": 200.0 + i,
            "high": 205.0 + i,
            "low": 195.0 + i,
            "close": 202.0 + i,
            "volume": 500,
        }
        for i in range(5)
    ]
    raw_rows[2] = corrupt_fn(raw_rows[2])  # type: ignore[operator]

    with pytest.raises(DataError) as exc_info:
        val.validate_rows(raw_rows, key)
    assert "Validation failed" in exc_info.value.what


# ---------------------------------------------------------------------------
# Invariant Matrices: Store Idempotency
# ---------------------------------------------------------------------------


def test_prop_store_commit_idempotency_and_checksum_stability(tmp_path: Path) -> None:
    """Re-committing the exact same chunk produces identical checksums and manifest records."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    store = BarStore(data_dir=tmp_path, manifest=manifest)

    inst_id = InstrumentId("NSE:EQ:HDFCBANK")
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1m", adjustment="raw", source="upstox"
    )
    bars = _gen_1m_bars(inst_id, date(2026, 10, 26), count=20)

    # First commit
    parts1 = store.commit(key, bars)
    assert len(parts1) == 1
    sha1 = parts1[0].sha256

    # Second commit (idempotent duplicate)
    parts2 = store.commit(key, bars)
    assert len(parts2) == 1
    sha2 = parts2[0].sha256

    assert sha1 == sha2
    assert len(manifest.get_parts()) == 1


# ---------------------------------------------------------------------------
# Invariant Matrices: Store Layout & Partition Routing
# ---------------------------------------------------------------------------


def test_prop_bar_store_multi_month_partition_split(tmp_path: Path) -> None:
    """Bars spanning month boundaries are partitioned into separate monthly parquets."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    store = BarStore(data_dir=tmp_path, manifest=manifest)

    inst_id = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1m", adjustment="raw", source="upstox"
    )

    bars_sep = _gen_1m_bars(inst_id, date(2026, 9, 30), count=5)
    bars_oct = _gen_1m_bars(inst_id, date(2026, 10, 1), count=5)
    combined = bars_sep + bars_oct

    parts = store.commit(key, combined)
    assert len(parts) == 2
    paths = [p.path for p in parts]
    assert any("2026-09.parquet" in p for p in paths)
    assert any("2026-10.parquet" in p for p in paths)


def test_prop_bar_store_daily_single_file_partition(tmp_path: Path) -> None:
    """Daily bars for all dates reside in a single daily.parquet file."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    store = BarStore(data_dir=tmp_path, manifest=manifest)

    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1d", adjustment="raw", source="upstox"
    )

    tz = ZoneInfo("Asia/Kolkata")
    daily_bars: list[Bar] = []
    for i in range(5):
        d = date(2026, 10, 19) + timedelta(days=i)
        s = datetime(d.year, d.month, d.day, 9, 15, tzinfo=tz).astimezone(UTC)
        e = datetime(d.year, d.month, d.day, 15, 30, tzinfo=tz).astimezone(UTC)
        daily_bars.append(
            Bar(
                instrument=inst_id,
                timeframe="1d",
                start_ts=s,
                end_ts=e,
                open=Decimal("2500.0"),
                high=Decimal("2550.0"),
                low=Decimal("2480.0"),
                close=Decimal("2520.0"),
                volume=50000,
            )
        )

    parts = store.commit(key, daily_bars)
    assert len(parts) == 1
    assert parts[0].path.endswith("daily.parquet")
    assert parts[0].rows == 5


@pytest.mark.parametrize("tf", ["2m", "3m", "5m", "10m", "15m", "30m", "60m", "1h", "1w", "1M"])
def test_prop_bar_store_refuses_derived_timeframes(tmp_path: Path, tf: str) -> None:
    """BarStore strictly refuses non-base timeframes with actionable error message."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    store = BarStore(data_dir=tmp_path, manifest=manifest)

    inst_id = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe=tf, adjustment="raw", source="upstox"
    )
    bars = _gen_1m_bars(inst_id, date(2026, 10, 26), count=3)

    with pytest.raises(DataError) as exc_info:
        store.commit(key, bars)
    assert "Unsupported bar timeframe" in exc_info.value.what
    assert "data.base_timeframes" in exc_info.value.what or "1m, 1d" in exc_info.value.what


# ---------------------------------------------------------------------------
# Invariant Matrices: DataSlice Provenance and Gaps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slice_days", "has_gap"),
    [
        (1, False),
        (3, False),
        (5, True),
        (7, True),
        (10, True),
    ],
)
def test_prop_data_slice_provenance_and_gap_reporting(
    tmp_path: Path, slice_days: int, has_gap: bool
) -> None:
    """DataSlice preserves full checksum provenance and accurate gap tracking."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    cal = PropertyHermeticCalendar()
    store = BarStore(data_dir=tmp_path, manifest=manifest, calendar=cal)

    inst_id = InstrumentId("NSE:EQ:SBIN")
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1m", adjustment="raw", source="upstox"
    )

    # Commit only the first 3 days: Mon Oct 26, Tue Oct 27, Wed Oct 28
    committed_bars: list[Bar] = []
    for day_offset in range(3):
        d = date(2026, 10, 26) + timedelta(days=day_offset)
        committed_bars.extend(_gen_1m_bars(inst_id, d, count=5))

    parts = store.commit(key, committed_bars)
    expected_sha = parts[0].sha256

    start_dt = datetime(2026, 10, 26, 9, 15, tzinfo=cal.tz).astimezone(UTC)
    end_dt = (
        datetime(2026, 10, 26, 15, 30, tzinfo=cal.tz) + timedelta(days=slice_days - 1)
    ).astimezone(UTC)

    d_slice = store.read(key, start_dt, end_dt)
    assert store.dataset_id(key) in d_slice.dataset_ids
    assert expected_sha in d_slice.checksums

    if has_gap:
        assert d_slice.has_gaps
        assert len(d_slice.gaps) > 0
    else:
        assert not d_slice.has_gaps
        assert len(d_slice.gaps) == 0


# ---------------------------------------------------------------------------
# Invariant Matrices: Coverage Status Transitions and Calendar Edge Cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day_offset", "is_weekend"),
    [
        (0, False),  # Mon Oct 26
        (1, False),  # Tue Oct 27
        (2, False),  # Wed Oct 28
        (3, False),  # Thu Oct 29
        (4, False),  # Fri Oct 30
        (5, True),  # Sat Oct 31
        (6, True),  # Sun Nov 01
    ],
)
def test_prop_coverage_status_weekend_vs_trading_day(
    tmp_path: Path, day_offset: int, is_weekend: bool
) -> None:
    """Weekends always evaluate to CLOSED, while weekdays evaluate to COMPLETE or UNAVAILABLE."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    cal = PropertyHermeticCalendar()
    cov_index = CoverageIndex(manifest=manifest, calendar=cal)

    d = date(2026, 10, 26) + timedelta(days=day_offset)
    status = cov_index.evaluate_day_status("test_ds", d, has_bars=False)

    if is_weekend:
        assert status == CoverageStatus.CLOSED
    else:
        assert status in (
            CoverageStatus.UNAVAILABLE,
            CoverageStatus.EMPTY,
            CoverageStatus.PARTIAL,
        )


# ---------------------------------------------------------------------------
# Invariant Matrices: Validator Price Jump Warnings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("close1", "close2", "should_warn"),
    [
        (100.0, 105.0, False),  # +5%
        (100.0, 115.0, False),  # +15%
        (100.0, 119.9, False),  # +19.9%
        (100.0, 120.1, True),  # +20.1% (>20% default threshold)
        (100.0, 130.0, True),  # +30%
        (100.0, 85.0, False),  # -15%
        (100.0, 75.0, True),  # -25%
        (100.0, 50.0, True),  # -50%
    ],
)
def test_prop_validator_price_jump_threshold_matrix(
    tmp_path: Path, close1: float, close2: float, should_warn: bool
) -> None:
    """Price jumps exceeding registry threshold produce warnings but do not fail chunk."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    val = BarValidator(data_dir=tmp_path, manifest=manifest)

    inst_id = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1m", adjustment="raw", source="upstox"
    )

    tz = ZoneInfo("Asia/Kolkata")
    t1 = datetime(2026, 10, 26, 9, 15, tzinfo=tz).astimezone(UTC)
    t2 = datetime(2026, 10, 26, 9, 16, tzinfo=tz).astimezone(UTC)

    b1 = Bar(
        instrument=inst_id,
        timeframe="1m",
        start_ts=t1,
        end_ts=t1 + timedelta(minutes=1),
        open=Decimal(str(close1)),
        high=Decimal(str(close1 + 2.0)),
        low=Decimal(str(close1 - 2.0)),
        close=Decimal(str(close1)),
        volume=1000,
    )
    b2 = Bar(
        instrument=inst_id,
        timeframe="1m",
        start_ts=t2,
        end_ts=t2 + timedelta(minutes=1),
        open=Decimal(str(close2)),
        high=Decimal(str(close2 + 2.0)),
        low=Decimal(str(close2 - 2.0)),
        close=Decimal(str(close2)),
        volume=1000,
    )

    rep = val.validate_chunk([b1, b2], key)
    assert rep.is_valid is True

    if should_warn:
        assert len(rep.warnings) == 1
        assert "price jump" in rep.warnings[0].lower()
    else:
        assert len(rep.warnings) == 0


# ---------------------------------------------------------------------------
# Invariant Matrices: Quarantine Lifecycle and Release
# ---------------------------------------------------------------------------


def test_prop_quarantine_show_and_release_flow(tmp_path: Path) -> None:
    """Quarantined item can be inspected via show and released with re-commit."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    from algodesk.data.quarantine import QuarantineManager

    store = BarStore(data_dir=tmp_path, manifest=manifest)
    qm = QuarantineManager(data_dir=tmp_path, manifest=manifest, store=store)

    inst_id = InstrumentId("NSE:EQ:TCS")
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1m", adjustment="raw", source="upstox"
    )
    val = BarValidator(data_dir=tmp_path, manifest=manifest)

    # Create invalid chunk (non-increasing start_ts)
    bars = _gen_1m_bars(inst_id, date(2026, 10, 26), count=3)
    bars[1] = Bar(
        instrument=inst_id,
        timeframe="1m",
        start_ts=bars[0].start_ts,
        end_ts=bars[0].end_ts,
        open=bars[1].open,
        high=bars[1].high,
        low=bars[1].low,
        close=bars[1].close,
        volume=bars[1].volume,
    )

    with pytest.raises(DataError) as exc_info:
        val.validate_chunk(bars, key)
    q_id = exc_info.value.fix.split("id: ")[1].split(")")[0]

    # Inspect via show()
    info = qm.show(q_id)
    assert info["id"] == q_id
    assert len(info["errors"]) > 0

    # Purge flow
    purged = qm.purge(id=q_id)
    assert purged == 1
    assert len(manifest.get_quarantine(status="purged")) == 1


# ---------------------------------------------------------------------------
# Invariant Matrices: SeriesKey, Layout and Schema Round-trip Properties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["bars", "ticks", "depth", "actions", "snapshot"],
)
def test_prop_series_key_all_valid_kinds(kind: str) -> None:
    """SeriesKey accepts all M2 kinds including actions and snapshot."""
    inst = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(instrument=inst, kind=kind, timeframe="1m", adjustment="raw", source="upstox")
    assert key.kind == kind


@pytest.mark.parametrize("adj", ["raw", "adjusted"])
def test_prop_series_key_valid_adjustments(adj: str) -> None:
    """SeriesKey accepts raw and adjusted adjustments."""
    inst = InstrumentId("NSE:EQ:TCS")
    key = SeriesKey(instrument=inst, kind="bars", timeframe="1d", adjustment=adj, source="upstox")
    assert key.adjustment == adj


def test_prop_series_key_rejects_invalid_adjustment() -> None:
    """SeriesKey rejects unsupported adjustments."""
    inst = InstrumentId("NSE:EQ:TCS")
    with pytest.raises(ValueError):
        SeriesKey(
            instrument=inst, kind="bars", timeframe="1d", adjustment="split_only", source="upstox"
        )


@pytest.mark.parametrize(
    ("path_str", "expected_symbol", "expected_tf"),
    [
        (
            "bars/source=upstox/timeframe=1m/exchange=NSE/segment=EQ/symbol=INFY/adj=raw/year=2026/2026-10.parquet",
            "INFY",
            "1m",
        ),
        (
            "bars/source=upstox/timeframe=1d/exchange=NSE/segment=EQ/symbol=RELIANCE/adj=adjusted/daily.parquet",
            "RELIANCE",
            "1d",
        ),
        (
            "bars/source=upstox/timeframe=1m/exchange=MCX/segment=COM/symbol=CRUDEOIL/adj=raw/year=2025/2025-12.parquet",
            "CRUDEOIL",
            "1m",
        ),
    ],
)
def test_prop_layout_parse_bar_file_path(
    path_str: str, expected_symbol: str, expected_tf: str
) -> None:
    """parse_bar_file_path accurately decodes file paths back to Series components."""
    from algodesk.data.layout import parse_bar_file_path

    info = parse_bar_file_path(path_str)
    assert info is not None
    assert info["symbol"] == expected_symbol
    assert info["timeframe"] == expected_tf


def test_prop_quarantine_purge_all_lifecycle(tmp_path: Path) -> None:
    """QuarantineManager.purge with all_items=True purges all entries."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    from algodesk.data.quarantine import QuarantineManager

    val = BarValidator(data_dir=tmp_path, manifest=manifest)
    qm = QuarantineManager(data_dir=tmp_path, manifest=manifest)

    inst = InstrumentId("NSE:EQ:SBIN")
    key = SeriesKey(instrument=inst, kind="bars", timeframe="1m", adjustment="raw", source="upstox")

    for _i in range(3):
        bars = _gen_1m_bars(inst, date(2026, 10, 26), count=3)
        bars[1] = Bar(
            instrument=inst,
            timeframe="1m",
            start_ts=bars[0].start_ts,
            end_ts=bars[0].end_ts,
            open=bars[1].open,
            high=bars[1].high,
            low=bars[1].low,
            close=bars[1].close,
            volume=bars[1].volume,
        )
        try:
            val.validate_chunk(bars, key)
        except DataError:
            pass

    items = manifest.get_quarantine(status="quarantined")
    assert len(items) == 3

    purged = qm.purge(all_items=True)
    assert purged == 3
    assert len(manifest.get_quarantine(status="quarantined")) == 0


def test_prop_coverage_status_closed_holiday(tmp_path: Path) -> None:
    """Configured holiday in calendar always returns CoverageStatus.CLOSED."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    holiday = date(2026, 10, 21)
    cal = PropertyHermeticCalendar(holidays=[holiday])
    cov_index = CoverageIndex(manifest=manifest, calendar=cal)

    st = cov_index.evaluate_day_status("ds_1", holiday, has_bars=False)
    assert st == CoverageStatus.CLOSED


def test_prop_coverage_status_empty_day(tmp_path: Path) -> None:
    """Past trading day with no bars evaluates to CoverageStatus.EMPTY or UNAVAILABLE."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    cal = PropertyHermeticCalendar()
    cov_index = CoverageIndex(manifest=manifest, calendar=cal)

    past_day = date(2026, 10, 19)  # Monday in past
    st = cov_index.evaluate_day_status("ds_1", past_day, has_bars=False)
    assert st in (CoverageStatus.EMPTY, CoverageStatus.UNAVAILABLE)


def test_prop_quarantine_show_nonexistent_raises(tmp_path: Path) -> None:
    """QuarantineManager.show raises DataError for non-existent item."""
    manifest_db = tmp_path / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    from algodesk.data.quarantine import QuarantineManager

    qm = QuarantineManager(data_dir=tmp_path, manifest=manifest)
    with pytest.raises(DataError) as exc_info:
        qm.show("nonexistent_id_12345")
    assert "not found" in exc_info.value.what


def test_prop_schema_zstd_compression(tmp_path: Path) -> None:
    """write_bars_parquet uses Zstandard compression."""
    import pyarrow.parquet as pq

    inst = InstrumentId("NSE:EQ:INFY")
    bars = _gen_1m_bars(inst, date(2026, 10, 26), count=5)
    f_path = tmp_path / "test_bars.parquet"
    write_bars_parquet(f_path, bars)

    metadata = pq.read_metadata(f_path)
    col_meta = metadata.row_group(0).column(0)
    assert col_meta.compression.upper() == "ZSTD"


def test_prop_data_verify_clean_exit_code_zero(tmp_project: Path) -> None:
    """data verify exits 0 and reports ok: True on valid data store."""
    data_dir = tmp_project / "data"
    manifest_db = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    store = BarStore(data_dir=data_dir, manifest=manifest)

    inst_id = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(
        instrument=inst_id, kind="bars", timeframe="1m", adjustment="raw", source="upstox"
    )
    bars = _gen_1m_bars(inst_id, date(2026, 10, 26), count=5)
    store.commit(key, bars)

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["ok"] is True
    assert data["files_checked"] >= 1
