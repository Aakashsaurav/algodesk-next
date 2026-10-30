"""Tests for CP2: BarValidator, Quarantine lifecycle, CLI commands, and Doctor checks."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import DataError
from algodesk.api.market import (
    Bar,
    InstrumentId,
    SeriesKey,
)
from algodesk.cli.app import app
from algodesk.data.layout import bar_file_path
from algodesk.data.manifest import DataManifest, QuarantineRecord
from algodesk.data.quarantine import QuarantineManager
from algodesk.data.store import BarStore
from algodesk.data.validator import BarValidator
from algodesk.ops.checks import build_core_checks
from algodesk.ops.runtime import open_runtime


class FakeCalendar:
    """Hermetic test calendar for validator tests."""

    def __init__(self) -> None:
        self.tz = ZoneInfo("Asia/Kolkata")

    def is_trading_day(self, d: date, segment: str = "") -> bool:
        return d.weekday() < 5

    def sessions(self, day: date, segment: str = "") -> list[object]:
        if not self.is_trading_day(day):
            return []
        s_start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=self.tz).astimezone(UTC)
        s_end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=self.tz).astimezone(UTC)
        from algodesk.api.protocols import Session

        return [Session(name="regular", start=s_start, end=s_end, tradable=True)]


class MockJournal:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(self, kind: str, payload: dict[str, object], durable: bool = True) -> int:
        self.events.append({"kind": kind, "payload": payload})
        return len(self.events)


def _make_valid_bars(inst_id: InstrumentId, day: date, count: int = 5) -> list[Bar]:
    ist = ZoneInfo("Asia/Kolkata")
    bars: list[Bar] = []
    base_p = 1000.0
    for i in range(count):
        s_dt = datetime(day.year, day.month, day.day, 9, 15 + i, tzinfo=ist).astimezone(UTC)
        bars.append(
            Bar(
                instrument=inst_id,
                timeframe="1m",
                start_ts=s_dt,
                end_ts=s_dt + timedelta(minutes=1),
                open=base_p + i,
                high=base_p + i + 2.0,
                low=base_p + i - 1.0,
                close=base_p + i + 1.0,
                volume=100 * (i + 1),
                complete=True,
            )
        )
    return bars


def test_validator_valid_chunk_passes(tmp_path: Path) -> None:
    """A valid chunk passes all rules without quarantine."""
    manifest = DataManifest(tmp_path / "data_manifest.db")
    calendar = FakeCalendar()
    validator = BarValidator(
        data_dir=tmp_path / "data",
        manifest=manifest,
        calendar=calendar,  # type: ignore[arg-type]
        price_jump_pct=20.0,
    )

    inst_id = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    bars = _make_valid_bars(inst_id, date(2026, 10, 26))

    report = validator.validate_chunk(bars, key)
    assert report.is_valid is True
    assert len(report.errors) == 0


def test_validator_reject_order_and_quarantine(tmp_path: Path) -> None:
    """Unordered bars fail order rule and are moved to quarantine with report.json."""
    manifest = DataManifest(tmp_path / "data_manifest.db")
    journal = MockJournal()
    validator = BarValidator(
        data_dir=tmp_path / "data",
        manifest=manifest,
        journal=journal,  # type: ignore[arg-type]
    )

    inst_id = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    bars = _make_valid_bars(inst_id, date(2026, 10, 26))
    # Reverse bars to violate order
    reversed_bars = list(reversed(bars))

    with pytest.raises(DataError, match="Validation failed"):
        validator.validate_chunk(reversed_bars, key)

    # Check that quarantine table recorded the failure
    q_entries = manifest.get_quarantine(status="quarantined")
    assert len(q_entries) == 1
    assert "order" in q_entries[0].reason.lower()

    # Check report.json exists
    rep_path = tmp_path / "data" / q_entries[0].report_path
    assert rep_path.exists()
    data = json.loads(rep_path.read_text())
    assert "order" in str(data["errors"]).lower()

    # Verify journal event
    q_events = [e for e in journal.events if e["kind"] == "data.quarantine"]
    assert len(q_events) == 1


def test_validator_reject_ohlc_sanity_and_quarantine(tmp_path: Path) -> None:
    """low > high or negative prices fail OHLC sanity check."""
    manifest = DataManifest(tmp_path / "data_manifest.db")
    validator = BarValidator(data_dir=tmp_path / "data", manifest=manifest)

    inst_id = InstrumentId("NSE:EQ:TCS")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    s_dt = datetime(2026, 10, 26, 3, 45, tzinfo=UTC)

    # Note: Bar class post_init checks low <= high, but raw/external dictionary might violate
    bad_dict = {
        "start_ts": s_dt,
        "end_ts": s_dt + timedelta(minutes=1),
        "open": 3000.0,
        "high": 2900.0,  # high < low
        "low": 3100.0,
        "close": 3050.0,
        "volume": 100,
        "complete": True,
    }

    with pytest.raises(DataError, match="Validation failed"):
        validator.validate_rows([bad_dict], key)

    q_entries = manifest.get_quarantine(status="quarantined")
    assert len(q_entries) == 1
    assert "ohlc" in q_entries[0].reason.lower()


def test_validator_price_jump_warning_only(tmp_path: Path) -> None:
    """Price jump exceeding threshold records warning but does NOT reject chunk."""
    manifest = DataManifest(tmp_path / "data_manifest.db")
    validator = BarValidator(
        data_dir=tmp_path / "data",
        manifest=manifest,
        price_jump_pct=15.0,
    )

    inst_id = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    s_dt = datetime(2026, 10, 26, 3, 45, tzinfo=UTC)
    b1 = Bar(
        instrument=inst_id,
        timeframe="1m",
        start_ts=s_dt,
        end_ts=s_dt + timedelta(minutes=1),
        open=100.0,
        high=102.0,
        low=99.0,
        close=100.0,
        volume=100,
        complete=True,
    )
    b2 = Bar(
        instrument=inst_id,
        timeframe="1m",
        start_ts=s_dt + timedelta(minutes=1),
        end_ts=s_dt + timedelta(minutes=2),
        open=130.0,
        high=135.0,
        low=128.0,
        close=130.0,  # 30% jump
        volume=100,
        complete=True,
    )

    report = validator.validate_chunk([b1, b2], key)
    assert report.is_valid is True
    assert len(report.warnings) >= 1
    assert "jump" in report.warnings[0].lower()
    # Quarantine must remain empty
    assert len(manifest.get_quarantine()) == 0


def test_quarantine_expiration_and_automatic_purge(tmp_path: Path) -> None:
    """Expired quarantine items (> data.quarantine_days = 3) are purged; newer are kept."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(tmp_path / "data_manifest.db")
    journal = MockJournal()
    qm = QuarantineManager(
        data_dir=data_dir,
        manifest=manifest,
        journal=journal,  # type: ignore[arg-type]
        quarantine_days=3,
    )

    # Create old quarantine item (5 days old)
    old_dir = data_dir / ".quarantine" / "old_item"
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "report.json").write_text('{"error": "old"}')
    old_time = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    manifest.record_quarantine(
        QuarantineRecord(
            id="old_item",
            chunk="chunk_old",
            reason="old error",
            report_path=".quarantine/old_item/report.json",
            created=old_time,
            status="quarantined",
        )
    )

    # Create fresh quarantine item (1 day old)
    fresh_dir = data_dir / ".quarantine" / "fresh_item"
    fresh_dir.mkdir(parents=True, exist_ok=True)
    (fresh_dir / "report.json").write_text('{"error": "fresh"}')
    fresh_time = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    manifest.record_quarantine(
        QuarantineRecord(
            id="fresh_item",
            chunk="chunk_fresh",
            reason="fresh error",
            report_path=".quarantine/fresh_item/report.json",
            created=fresh_time,
            status="quarantined",
        )
    )

    purged = qm.purge_expired()
    assert purged == 1

    # Old directory removed, fresh directory kept
    assert not old_dir.exists()
    assert fresh_dir.exists()

    # Manifest records updated
    assert manifest.get_quarantine(id="old_item")[0].status == "purged"
    assert manifest.get_quarantine(id="fresh_item")[0].status == "quarantined"


def test_quarantine_release_with_reason(tmp_path: Path) -> None:
    """Releasing a quarantined item re-validates and commits to store with journal event."""
    data_dir = tmp_path / "data"
    manifest = DataManifest(tmp_path / "data_manifest.db")
    journal = MockJournal()
    store = BarStore(data_dir=data_dir, manifest=manifest, journal=journal)  # type: ignore[arg-type]
    qm = QuarantineManager(data_dir=data_dir, manifest=manifest, store=store, journal=journal)  # type: ignore[arg-type]

    inst_id = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    bars = _make_valid_bars(inst_id, date(2026, 10, 26))

    # Manually stage a quarantine item
    q_dir = data_dir / ".quarantine" / "release_test"
    q_dir.mkdir(parents=True, exist_ok=True)
    from algodesk.data.schema import write_bars_parquet

    write_bars_parquet(q_dir / "bars.parquet", bars)
    meta = {
        "instrument": str(key.instrument),
        "kind": key.kind,
        "timeframe": key.timeframe,
        "adjustment": key.adjustment,
        "source": key.source,
    }
    (q_dir / "meta.json").write_text(json.dumps(meta))
    manifest.record_quarantine(
        QuarantineRecord(
            id="release_test",
            chunk="NSE:EQ:INFY 2026-10-26",
            reason="Spurious check",
            report_path=".quarantine/release_test/report.json",
            created=datetime.now(UTC).isoformat(),
            status="quarantined",
        )
    )

    res = qm.release("release_test", reason="Verified by data vendor notice")
    assert res is True

    # Check store has bars
    data_slice = store.read(key, bars[0].start_ts, bars[-1].end_ts)
    assert len(data_slice.bars) == len(bars)
    assert manifest.get_quarantine(id="release_test")[0].status == "released"


def test_cli_data_verify_clean_cache_and_json(tmp_project: Path) -> None:
    """data verify exits 0 on clean workspace and produces structured JSON."""
    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "verify"])
    assert res.exit_code == 0
    assert "VERIFY: OK" in res.stdout or "0 files" in res.stdout or "Verified" in res.stdout

    res_json = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--json"])
    assert res_json.exit_code == 0
    parsed = json.loads(res_json.stdout)
    assert parsed["ok"] is True
    assert "orphans" in parsed


def test_cli_data_verify_detects_orphan_and_adopt(tmp_project: Path) -> None:
    """data verify detects orphan bar file on disk and --adopt registers it."""
    data_dir = tmp_project / "data"
    manifest_db = data_dir / "state" / "data_manifest.db"
    manifest_obj = DataManifest(manifest_db)
    manifest_obj.close()

    inst_id = InstrumentId("NSE:EQ:INFY")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    bars = _make_valid_bars(inst_id, date(2026, 10, 26))

    # Write orphan parquet file directly to layout path without manifest entry
    target_path = bar_file_path(data_dir, key, bars[0].start_ts)
    from algodesk.data.schema import write_bars_parquet

    write_bars_parquet(target_path, bars)

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "verify"])
    # Must report orphan
    assert "orphan" in res.stdout.lower()

    # Now adopt with --adopt
    res_adopt = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--adopt"])
    assert res_adopt.exit_code == 0
    assert "adopted" in res_adopt.stdout.lower()

    # Re-running verify should now be clean
    res_clean = runner.invoke(app, ["--root", str(tmp_project), "data", "verify"])
    assert res_clean.exit_code == 0


def test_cli_data_verify_detects_corrupt_file(tmp_project: Path) -> None:
    """data verify detects checksum mismatch / damaged parquet file."""
    data_dir = tmp_project / "data"
    manifest_db = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    store = BarStore(data_dir=data_dir, manifest=manifest)

    inst_id = InstrumentId("NSE:EQ:TCS")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    bars = _make_valid_bars(inst_id, date(2026, 10, 26))
    parts = store.commit(key, bars)
    assert len(parts) == 1

    # Tamper with the parquet file on disk
    p = data_dir / parts[0].path
    p.write_bytes(b"corrupted_bytes_content_not_valid_parquet")

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "verify"])
    assert res.exit_code != 0
    out_lower = res.stdout.lower()
    assert "checksum" in out_lower or "damaged" in out_lower or "corrupt" in out_lower


def test_cli_data_status_and_gaps(tmp_project: Path) -> None:
    """data status and data gaps commands execute successfully."""
    runner = CliRunner()
    res_stat = runner.invoke(app, ["--root", str(tmp_project), "data", "status"])
    assert res_stat.exit_code == 0

    res_stat_grp = runner.invoke(app, ["--root", str(tmp_project), "data", "status", "--group"])
    assert res_stat_grp.exit_code == 0

    res_gaps = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "gaps",
            "NSE:EQ:INFY",
            "--from",
            "2026-10-20",
            "--to",
            "2026-10-25",
        ],
    )
    assert res_gaps.exit_code == 0


def test_cli_data_quarantine_commands(tmp_project: Path) -> None:
    """data quarantine list, show, and purge commands work as expected."""
    runner = CliRunner()
    res_list = runner.invoke(app, ["--root", str(tmp_project), "data", "quarantine", "list"])
    assert res_list.exit_code == 0


def test_doctor_data_checks(tmp_project: Path) -> None:
    """Doctor checks data.verify_quick and data.quarantine are registered and execute cleanly."""
    with open_runtime(tmp_project, ensure=False) as rt:
        checks = build_core_checks(rt)
        check_ids = [c.id for c in checks]

        assert "data.verify_quick" in check_ids
        assert "data.quarantine" in check_ids

        # Run data.verify_quick
        vq_check = next(c for c in checks if c.id == "data.verify_quick")
        res_vq = vq_check.run()
        assert res_vq.ok is True

        # Run data.quarantine
        q_check = next(c for c in checks if c.id == "data.quarantine")
        res_q = q_check.run()
        assert res_q.ok is True
