"""Tests for Step 11: SessionBarStore and live session-bar separation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api.market import Bar, InstrumentId
from algodesk.cli.app import app
from algodesk.data.session_bars import SessionBarStore


def _make_sample_bars(
    inst: InstrumentId, d: date, count: int = 5, start_hour: int = 9, start_min: int = 15
) -> list[Bar]:
    tz = ZoneInfo("Asia/Kolkata")
    bars: list[Bar] = []
    base_price = Decimal("100.00")
    for i in range(count):
        m = start_min + i
        s_dt = datetime(d.year, d.month, d.day, start_hour, m, tzinfo=tz).astimezone(UTC)
        e_dt = s_dt + timedelta(minutes=1)
        bars.append(
            Bar(
                instrument=inst,
                timeframe="1m",
                start_ts=s_dt,
                end_ts=e_dt,
                open=base_price + Decimal(str(i)),
                high=base_price + Decimal(str(i)) + Decimal("1.0"),
                low=base_price + Decimal(str(i)) - Decimal("0.5"),
                close=base_price + Decimal(str(i)) + Decimal("0.2"),
                volume=1000 + i * 10,
            )
        )
    return bars


class DummyParamReader:
    def __init__(self, keep_days: int = 2) -> None:
        self.keep_days = keep_days

    def get(self, key: str) -> str:
        if key == "data.session_bars_keep_days":
            return str(self.keep_days)
        raise KeyError(key)


def test_session_bar_store_init_and_defaults(tmp_path: Path) -> None:
    """SessionBarStore defaults to 1 keep day and respects explicit or param values."""
    sbs_def = SessionBarStore(tmp_path)
    assert sbs_def.keep_days == 1
    assert sbs_def.base_dir == tmp_path / "session_bars"

    sbs_expl = SessionBarStore(tmp_path, keep_days=5)
    assert sbs_expl.keep_days == 5

    sbs_param = SessionBarStore(tmp_path, params=DummyParamReader(keep_days=3))
    assert sbs_param.keep_days == 3


def test_session_bar_store_path_layout(tmp_path: Path) -> None:
    """SessionBarStore resolves path at session_bars/date=YYYY-MM-DD/<symbol>.parquet."""
    sbs = SessionBarStore(tmp_path)
    inst = InstrumentId("NSE:EQ:INFY")
    d = date(2026, 10, 26)
    expected = tmp_path / "session_bars" / "date=2026-10-26" / "INFY.parquet"
    assert sbs.path_for(d, inst) == expected


def test_session_bar_record_and_load(tmp_path: Path) -> None:
    """Session bars can be persisted and loaded cleanly."""
    sbs = SessionBarStore(tmp_path)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    d = date(2026, 10, 26)
    bars = _make_sample_bars(inst, d, count=10)

    p = sbs.record_bars(d, inst, bars)
    assert p.is_file()
    assert p.name == "RELIANCE.parquet"
    assert "date=2026-10-26" in str(p)

    loaded = sbs.load_bars(d, inst, timeframe="1m")
    assert len(loaded) == 10
    assert loaded[0].instrument == inst
    assert float(loaded[0].open) == float(bars[0].open)
    assert float(loaded[-1].close) == float(bars[-1].close)


def test_session_bar_isolation_from_historical_bars(tmp_path: Path) -> None:
    """Writing session bars leaves data/bars/ and manifest untouched."""
    data_dir = tmp_path / "data"
    sbs = SessionBarStore(data_dir)
    inst = InstrumentId("NSE:EQ:TCS")
    d = date(2026, 10, 26)
    bars = _make_sample_bars(inst, d, count=5)

    sbs.record_bars(d, inst, bars)

    assert (data_dir / "session_bars").exists()
    assert not (data_dir / "bars").exists()
    assert not (data_dir / "state" / "data_manifest.db").exists()


def test_session_bar_load_nonexistent(tmp_path: Path) -> None:
    """Loading nonexistent session bars returns empty list."""
    sbs = SessionBarStore(tmp_path)
    inst = InstrumentId("NSE:EQ:NONEXISTENT")
    loaded = sbs.load_bars(date(2026, 10, 26), inst)
    assert loaded == []


def test_session_bar_purge_older_than(tmp_path: Path) -> None:
    """SessionBarStore.purge_older_than removes partitions older than cutoff."""
    sbs = SessionBarStore(tmp_path, keep_days=2)
    inst = InstrumentId("NSE:EQ:SBIN")
    today = datetime.now(UTC).date()

    d_today = today
    d_yesterday = today - timedelta(days=1)
    d_old3 = today - timedelta(days=3)
    d_old5 = today - timedelta(days=5)

    sbs.record_bars(d_today, inst, _make_sample_bars(inst, d_today))
    sbs.record_bars(d_yesterday, inst, _make_sample_bars(inst, d_yesterday))
    sbs.record_bars(d_old3, inst, _make_sample_bars(inst, d_old3))
    sbs.record_bars(d_old5, inst, _make_sample_bars(inst, d_old5))

    purged = sbs.purge_older_than(days=2)
    assert purged == 2

    assert sbs.path_for(d_today, inst).exists()
    assert sbs.path_for(d_yesterday, inst).exists()
    assert not sbs.path_for(d_old3, inst).exists()
    assert not sbs.path_for(d_old5, inst).exists()


def test_session_bar_purge_nonexistent_base_dir(tmp_path: Path) -> None:
    """Purge returns 0 when session_bars directory does not exist."""
    sbs = SessionBarStore(tmp_path / "empty_dir")
    assert sbs.purge_older_than() == 0


def test_session_bar_purge_ignores_non_date_folders(tmp_path: Path) -> None:
    """Purge ignores extraneous non-date directories inside session_bars."""
    sbs = SessionBarStore(tmp_path)
    base = sbs.base_dir
    base.mkdir(parents=True, exist_ok=True)
    misc_dir = base / "misc_backup"
    misc_dir.mkdir()
    (misc_dir / "some_file.txt").write_text("hello")

    purged = sbs.purge_older_than(days=0)
    assert purged == 0
    assert misc_dir.exists()


def test_session_bar_multiple_symbols_same_date(tmp_path: Path) -> None:
    """Multiple instruments on the same session date coexist in the partition."""
    sbs = SessionBarStore(tmp_path)
    d = date(2026, 10, 26)
    inst1 = InstrumentId("NSE:EQ:INFY")
    inst2 = InstrumentId("NSE:EQ:WIPRO")

    sbs.record_bars(d, inst1, _make_sample_bars(inst1, d, count=3))
    sbs.record_bars(d, inst2, _make_sample_bars(inst2, d, count=4))

    bars1 = sbs.load_bars(d, inst1)
    bars2 = sbs.load_bars(d, inst2)

    assert len(bars1) == 3
    assert len(bars2) == 4
    assert bars1[0].instrument == inst1
    assert bars2[0].instrument == inst2


def test_session_bar_overwrite_same_day(tmp_path: Path) -> None:
    """Re-recording for the same symbol on the same day cleanly overwrites."""
    sbs = SessionBarStore(tmp_path)
    d = date(2026, 10, 26)
    inst = InstrumentId("NSE:EQ:HDFCBANK")

    sbs.record_bars(d, inst, _make_sample_bars(inst, d, count=3))
    assert len(sbs.load_bars(d, inst)) == 3

    sbs.record_bars(d, inst, _make_sample_bars(inst, d, count=7))
    assert len(sbs.load_bars(d, inst)) == 7


@pytest.mark.parametrize(
    ("raw_id", "symbol"),
    [
        ("NSE:EQ:RELIANCE", "RELIANCE"),
        ("BSE:EQ:INFY", "INFY"),
        ("MCX:COM:GOLD", "GOLD"),
        ("NSE:FO:NIFTY26OCTFUT", "NIFTY26OCTFUT"),
    ],
)
def test_session_bar_instrument_formats(tmp_path: Path, raw_id: str, symbol: str) -> None:
    """Different exchange and segment instrument IDs map correctly to symbol files."""
    sbs = SessionBarStore(tmp_path)
    inst = InstrumentId(raw_id)
    d = date(2026, 10, 26)
    bars = _make_sample_bars(inst, d, count=2)
    p = sbs.record_bars(d, inst, bars)
    assert p.name == f"{symbol}.parquet"
    loaded = sbs.load_bars(d, inst)
    assert len(loaded) == 2


def test_cli_data_session_bars_purge(tmp_project: Path) -> None:
    """CLI data session-bars purge command removes old date folders."""
    data_dir = tmp_project / "data"
    sbs = SessionBarStore(data_dir)
    inst = InstrumentId("NSE:EQ:INFY")
    old_date = datetime.now(UTC).date() - timedelta(days=10)
    sbs.record_bars(old_date, inst, _make_sample_bars(inst, old_date, count=2))

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "session-bars", "purge"])
    assert res.exit_code == 0
    assert "Purged 1 session bar" in res.stdout
    assert not sbs.path_for(old_date, inst).exists()


def test_cli_data_session_bars_purge_with_days_flag(tmp_project: Path) -> None:
    """CLI data session-bars purge --days flag overrides default keep_days."""
    data_dir = tmp_project / "data"
    sbs = SessionBarStore(data_dir)
    inst = InstrumentId("NSE:EQ:INFY")
    d3 = datetime.now(UTC).date() - timedelta(days=3)
    sbs.record_bars(d3, inst, _make_sample_bars(inst, d3, count=2))

    runner = CliRunner()
    # keep_days = 5 => d3 is not purged
    res_keep = runner.invoke(
        app, ["--root", str(tmp_project), "data", "session-bars", "purge", "--days", "5"]
    )
    assert res_keep.exit_code == 0
    assert "Purged 0 session bar" in res_keep.stdout
    assert sbs.path_for(d3, inst).exists()

    # keep_days = 2 => d3 is purged
    res_purge = runner.invoke(
        app, ["--root", str(tmp_project), "data", "session-bars", "purge", "--days", "2"]
    )
    assert res_purge.exit_code == 0
    assert "Purged 1 session bar" in res_purge.stdout
    assert not sbs.path_for(d3, inst).exists()
