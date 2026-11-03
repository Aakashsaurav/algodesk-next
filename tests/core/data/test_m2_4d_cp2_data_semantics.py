"""Tests for data gaps semantics (MISSING vs EMPTY, series selection) and data verify with --fix."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from algodesk.api.market import Bar, CoverageStatus, InstrumentId, SeriesKey
from algodesk.cli.app import app
from algodesk.data.layout import bar_file_path, dataset_id_for_key
from algodesk.data.manifest import (
    CoverageDayRecord,
    DataManifest,
    DatasetRecord,
    PartRecord,
)
from algodesk.data.schema import write_bars_parquet
from algodesk.journal.store import events


def _make_bar(
    inst: InstrumentId,
    dt: datetime,
    o: float = 100.0,
    h: float = 100.0,
    l: float = 100.0,  # noqa: E741
    c: float = 100.0,
    v: int = 0,
    tf: str = "1d",
) -> Bar:
    return Bar(
        instrument=inst,
        timeframe=tf,
        start_ts=dt,
        end_ts=dt.replace(minute=dt.minute + 1 if dt.minute < 59 else 0),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=v,
    )


def test_data_gaps_distinguishes_missing_vs_empty_and_prints_checked_series(
    tmp_project: Path,
) -> None:
    """data gaps reports MISSING for unfetched days and EMPTY for fetched days with 0 rows."""
    data_dir = tmp_project / "data"
    manifest_db = data_dir / "manifest.db"
    manifest = DataManifest(manifest_db)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1d", "raw", "upstox")
    ds_id = dataset_id_for_key(key)

    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument=str(inst),
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )

    # 2026-10-27 is recorded as EMPTY in manifest (fetched and returned 0 bars)
    manifest.record_coverage_days(
        [
            CoverageDayRecord(
                dataset_id=ds_id,
                day="2026-10-27",
                status=CoverageStatus.EMPTY,
                rows=0,
                expected_rows=0,
                part_path="",
                fetched_at=datetime.now(UTC).isoformat(),
                note="Fetched and source returned nothing",
            )
        ]
    )
    manifest.close()

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "gaps",
            "RELIANCE",
            "--from",
            "2026-10-26",
            "--to",
            "2026-10-28",
            "--timeframe",
            "1d",
            "--source",
            "upstox",
            "--adj",
            "raw",
        ],
    )
    assert res.exit_code == 0
    # Must report which series was checked
    assert f"Checked series: {ds_id}" in res.output

    # 2026-10-27 was fetched and empty -> EMPTY
    assert "2026-10-27" in res.output
    assert "EMPTY" in res.output

    # 2026-10-26 and 2026-10-28 had no coverage record -> MISSING
    assert "2026-10-26" in res.output
    assert "2026-10-28" in res.output
    assert "MISSING" in res.output
    assert "No coverage record in manifest" in res.output


def test_data_gaps_series_selection_defaults_to_most_coverage(
    tmp_project: Path,
) -> None:
    """data gaps selects dataset with most coverage by default when flags are omitted."""
    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    inst = InstrumentId("NSE:EQ:INFY")

    # Series 1: raw upstox (1 coverage day)
    ds_raw = dataset_id_for_key(SeriesKey(inst, "bars", "1d", "raw", "upstox"))
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_raw,
            instrument=str(inst),
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="none",
        )
    )
    manifest.record_coverage_days(
        [
            CoverageDayRecord(
                dataset_id=ds_raw,
                day="2026-10-26",
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path="part1.parquet",
                fetched_at=datetime.now(UTC).isoformat(),
            )
        ]
    )

    # Series 2: adjusted upstox (3 coverage days -> more coverage)
    ds_adj = dataset_id_for_key(SeriesKey(inst, "bars", "1d", "adjusted", "upstox"))
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_adj,
            instrument=str(inst),
            kind="bars",
            timeframe="1d",
            adjustment="adjusted",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )
    manifest.record_coverage_days(
        [
            CoverageDayRecord(
                dataset_id=ds_adj,
                day=f"2026-10-{d}",
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path=f"part_{d}.parquet",
                fetched_at=datetime.now(UTC).isoformat(),
            )
            for d in ("26", "27", "28")
        ]
    )
    manifest.close()

    runner = CliRunner()
    # Default invocation without --adj -> must select adjusted series due to higher coverage
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "gaps",
            "INFY",
            "--from",
            "2026-10-26",
            "--to",
            "2026-10-29",
            "--timeframe",
            "1d",
        ],
    )
    assert res.exit_code == 0
    assert f"Checked series: {ds_adj}" in res.output

    # Explicit --adj raw selects raw series
    res_raw = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "gaps",
            "INFY",
            "--from",
            "2026-10-26",
            "--to",
            "2026-10-29",
            "--timeframe",
            "1d",
            "--adj",
            "raw",
        ],
    )
    assert res_raw.exit_code == 0
    assert f"Checked series: {ds_raw}" in res_raw.output


def test_data_verify_detects_flat_zero_volume_bars_and_fix_quarantines(
    tmp_project: Path,
) -> None:
    """Fixture reproduces evidence (d): flat zero-volume bars on 2025-03-18 and 2026-01-15
    are quarantined with --fix.
    """
    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "manifest.db")

    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1d", "raw", "upstox")
    ds_id = dataset_id_for_key(key)

    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument=str(inst),
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )

    # Tuesday 2025-03-18: Flat zero-volume phantom bar
    flat_bar_1 = _make_bar(
        inst, datetime(2025, 3, 18, 9, 15, tzinfo=UTC), o=1250.0, h=1250.0, l=1250.0, c=1250.0, v=0
    )
    # Wednesday 2025-03-19: Normal valid bar
    normal_bar_1 = _make_bar(
        inst,
        datetime(2025, 3, 19, 9, 15, tzinfo=UTC),
        o=1250.0,
        h=1270.0,
        l=1240.0,
        c=1260.0,
        v=50000,
    )
    # Thursday 2026-01-15: Flat zero-volume phantom bar
    flat_bar_2 = _make_bar(
        inst, datetime(2026, 1, 15, 9, 15, tzinfo=UTC), o=1400.0, h=1400.0, l=1400.0, c=1400.0, v=0
    )
    # Friday 2026-01-16: Normal valid bar
    normal_bar_2 = _make_bar(
        inst,
        datetime(2026, 1, 16, 9, 15, tzinfo=UTC),
        o=1400.0,
        h=1420.0,
        l=1390.0,
        c=1410.0,
        v=80000,
    )

    bars = [flat_bar_1, normal_bar_1, flat_bar_2, normal_bar_2]
    p_path = bar_file_path(data_dir, key, bars[0].start_ts)
    write_bars_parquet(p_path, bars)

    from algodesk.cli.data import _compute_sha256

    rel_path = str(p_path.relative_to(data_dir))
    sha = _compute_sha256(p_path)
    now_iso = datetime.now(UTC).isoformat()
    manifest.record_part(
        PartRecord(
            path=rel_path,
            dataset_id=ds_id,
            rows=len(bars),
            bytes=p_path.stat().st_size,
            first_ts=bars[0].start_ts.isoformat(),
            last_ts=bars[-1].start_ts.isoformat(),
            sha256=sha,
            source="upstox",
            source_version="1.0",
            fetched_at=now_iso,
        )
    )
    for b in bars:
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day=b.start_ts.date().isoformat(),
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path=rel_path,
                fetched_at=now_iso,
            )
        )
    manifest.close()

    runner = CliRunner()

    # Step 1: verify without --fix detects flat zero-volume bars and exits != 0
    res_verify = runner.invoke(
        app, ["--root", str(tmp_project), "data", "verify", "--only", "bars"]
    )
    assert res_verify.exit_code != 0
    assert "VERIFY: FAILED" in res_verify.output
    assert "Rule violations" in res_verify.output
    assert "2025-03-18" in res_verify.output or "flat zero-volume" in res_verify.output

    # Step 2: verify --fix removes the flat bars, moves them to quarantine, journals event
    res_fix = runner.invoke(
        app, ["--root", str(tmp_project), "data", "verify", "--only", "bars", "--fix"]
    )
    if res_fix.exit_code != 0:
        raise AssertionError(f"res_fix failed: code={res_fix.exit_code}, output={res_fix.output}")
    assert res_fix.exit_code == 0
    assert "VERIFY: OK" in res_fix.output
    assert "Quarantined 2 offending bar(s)" in res_fix.output

    # Step 3: Journal must contain data.quarantine event
    from algodesk.ops.paths import StatePaths

    state_paths = StatePaths.resolve(tmp_project, data_dir)
    journal_path = state_paths.journal_db
    assert journal_path.is_file()
    q_events = [e for e in events(journal_path) if e.get("kind") == "data.quarantine"]
    assert len(q_events) >= 1
    p_data = json.loads(q_events[0]["payload_json"])
    assert p_data["action"] == "quarantined"

    # Step 4: Re-running verify is clean
    res_clean = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--only", "bars"])
    assert res_clean.exit_code == 0
    assert "VERIFY: OK" in res_clean.output

    # Step 5: Clean bars remain in stored parquet, flat bars are gone
    from algodesk.data.schema import read_bars_parquet

    remaining_bars = read_bars_parquet(p_path, inst, "1d")
    assert len(remaining_bars) == 2
    rem_dates = [b.start_ts.date() for b in remaining_bars]
    assert date(2025, 3, 19) in rem_dates
    assert date(2026, 1, 16) in rem_dates
    assert date(2025, 3, 18) not in rem_dates
    assert date(2026, 1, 15) not in rem_dates


def test_coverage_status_missing_enum_value() -> None:
    """CoverageStatus enum contains MISSING and converts properly."""
    assert CoverageStatus.MISSING == "MISSING"
    assert CoverageStatus("MISSING") == CoverageStatus.MISSING
    assert str(CoverageStatus.MISSING) == "MISSING"


def test_data_gaps_json_output(tmp_project: Path) -> None:
    """data gaps --json outputs valid JSON containing series, day, status, and reason."""
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "gaps",
            "RELIANCE",
            "--from",
            "2026-10-26",
            "--to",
            "2026-10-27",
            "--timeframe",
            "1d",
            "--json",
        ],
    )
    assert res.exit_code == 0
    parsed = json.loads(res.output)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    for item in parsed:
        assert item["instrument"] == "NSE:EQ:RELIANCE"
        assert "series" in item
        assert "day" in item
        assert item["status"] == "MISSING"
        assert item["reason"] == "No coverage record in manifest"


def test_data_gaps_all_complete_reports_no_gaps(tmp_project: Path) -> None:
    """When all days in range are COMPLETE, data gaps reports no gaps found."""
    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    inst = InstrumentId("NSE:EQ:TCS")
    key = SeriesKey(inst, "bars", "1d", "raw", "upstox")
    ds_id = dataset_id_for_key(key)

    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument=str(inst),
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )
    manifest.record_coverage_days(
        [
            CoverageDayRecord(
                dataset_id=ds_id,
                day="2026-10-26",
                status=CoverageStatus.COMPLETE,
                rows=1,
                expected_rows=1,
                part_path="p.parquet",
                fetched_at=datetime.now(UTC).isoformat(),
            )
        ]
    )
    manifest.close()

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "gaps",
            "TCS",
            "--from",
            "2026-10-26",
            "--to",
            "2026-10-26",
            "--timeframe",
            "1d",
        ],
    )
    assert res.exit_code == 0
    assert "No gaps found for 'TCS'" in res.output


def test_data_verify_all_flat_bars_removes_part_completely(tmp_project: Path) -> None:
    """When all bars in a part are flat zero-volume, --fix deletes the part file
    and removes manifest part.
    """
    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "manifest.db")

    inst = InstrumentId("NSE:EQ:SBIN")
    key = SeriesKey(inst, "bars", "1d", "raw", "upstox")
    ds_id = dataset_id_for_key(key)

    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument=str(inst),
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )

    flat_bar = _make_bar(
        inst, datetime(2025, 3, 18, 9, 15, tzinfo=UTC), o=600.0, h=600.0, l=600.0, c=600.0, v=0
    )
    p_path = bar_file_path(data_dir, key, flat_bar.start_ts)
    write_bars_parquet(p_path, [flat_bar])

    from algodesk.cli.data import _compute_sha256

    rel_path = str(p_path.relative_to(data_dir))
    sha = _compute_sha256(p_path)
    now_iso = datetime.now(UTC).isoformat()
    manifest.record_part(
        PartRecord(
            path=rel_path,
            dataset_id=ds_id,
            rows=1,
            bytes=p_path.stat().st_size,
            first_ts=flat_bar.start_ts.isoformat(),
            last_ts=flat_bar.start_ts.isoformat(),
            sha256=sha,
            source="upstox",
            source_version="1.0",
            fetched_at=now_iso,
        )
    )
    manifest.close()

    runner = CliRunner()
    res_fix = runner.invoke(
        app, ["--root", str(tmp_project), "data", "verify", "--only", "bars", "--fix"]
    )
    assert res_fix.exit_code == 0
    assert "Quarantined 1 offending bar(s)" in res_fix.output

    # Part file on disk must be deleted
    assert not p_path.is_file()

    # Part record in manifest must be removed
    manifest_check = DataManifest(data_dir / "manifest.db")
    parts = manifest_check.get_parts(ds_id)
    manifest_check.close()
    assert len(parts) == 0


def test_data_verify_non_flat_zero_volume_bar_not_flagged(tmp_project: Path) -> None:
    """Zero volume bar where open != close is not treated as flat zero phantom bar."""
    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "manifest.db")

    inst = InstrumentId("NSE:EQ:HDFC")
    key = SeriesKey(inst, "bars", "1d", "raw", "upstox")
    ds_id = dataset_id_for_key(key)

    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument=str(inst),
            kind="bars",
            timeframe="1d",
            adjustment="raw",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )

    # open != close -> not flat
    b = _make_bar(
        inst, datetime(2025, 3, 18, 9, 15, tzinfo=UTC), o=1500.0, h=1520.0, l=1490.0, c=1510.0, v=0
    )
    p_path = bar_file_path(data_dir, key, b.start_ts)
    write_bars_parquet(p_path, [b])

    from algodesk.cli.data import _compute_sha256

    rel_path = str(p_path.relative_to(data_dir))
    sha = _compute_sha256(p_path)
    now_iso = datetime.now(UTC).isoformat()
    manifest.record_part(
        PartRecord(
            path=rel_path,
            dataset_id=ds_id,
            rows=1,
            bytes=p_path.stat().st_size,
            first_ts=b.start_ts.isoformat(),
            last_ts=b.start_ts.isoformat(),
            sha256=sha,
            source="upstox",
            source_version="1.0",
            fetched_at=now_iso,
        )
    )
    manifest.close()

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--only", "bars"])
    assert res.exit_code == 0
    assert "VERIFY: OK" in res.output


def test_data_verify_json_output_includes_quarantined_bars(tmp_project: Path) -> None:
    """data verify --json includes quarantined_bars in result dictionary."""
    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--json"])
    assert res.exit_code == 0
    parsed = json.loads(res.output)
    assert parsed["ok"] is True
    assert "quarantined_bars" in parsed
    assert parsed["quarantined_bars"] == 0
