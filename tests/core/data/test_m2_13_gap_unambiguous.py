"""Tests for M2-13: Unambiguous gap checks, CoverageIndex safety, and hashing."""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import tomli
from typer.testing import CliRunner

from algodesk.api.errors import DataError
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, CoverageStatus, InstrumentId, SeriesKey
from algodesk.cli.app import app
from algodesk.data.coverage import CoverageIndex
from algodesk.data.layout import bar_file_path, dataset_id_for_key
from algodesk.data.manifest import (
    CoverageDayRecord,
    DataManifest,
    DatasetRecord,
    PartRecord,
)
from algodesk.data.schema import write_bars_parquet
from algodesk.data.service import DataService
from algodesk.data.store import (
    BarStore,
    _compute_sha256,
    get_source_declared_intervals,
    validate_source_timeframe,
)
from algodesk.journal.store import events
from algodesk.registry.conformance import code_hash

_IST = ZoneInfo("Asia/Kolkata")


def _make_bar(
    inst: InstrumentId,
    dt: datetime,
    tf: str = "1d",
    close: float = 100.0,
    volume: int = 1000,
) -> Bar:
    return Bar(
        instrument=inst,
        timeframe=tf,
        start_ts=dt,
        end_ts=dt.replace(minute=dt.minute + 1 if tf == "1m" else dt.minute),
        open=Decimal(str(close)),
        high=Decimal(str(close + 1.0)),
        low=Decimal(str(close - 1.0)),
        close=Decimal(str(close)),
        volume=volume,
        complete=True,
    )


# -----------------------------------------------------------------------------
# Step 1 & 8: CoverageIndex callable safety and store property access
# -----------------------------------------------------------------------------


def test_step1_coverage_index_callable_returns_self(tmp_path: Path) -> None:
    """CoverageIndex instance invoked with no arguments returns self safely."""
    manifest = DataManifest(tmp_path / "manifest.db")
    cov = CoverageIndex(manifest=manifest, settle_minutes=15)
    # Invoking cov() without args must return cov itself (no TypeError)
    result = cov()
    assert result is cov


def test_step1_coverage_index_callable_evaluates_status(tmp_path: Path) -> None:
    """CoverageIndex instance invoked with arguments delegates to evaluate_day_status."""
    manifest = DataManifest(tmp_path / "manifest.db")
    cov = CoverageIndex(manifest=manifest, settle_minutes=15)
    status = cov("bars:upstox:1d:NSE:EQ:INFY:raw", date(2026, 11, 10), has_bars=True)
    assert status == CoverageStatus.COMPLETE


def test_step1_store_coverage_index_property_and_callable_access(tmp_path: Path) -> None:
    """BarStore.coverage_index is accessible as a property and callable without error."""
    manifest = DataManifest(tmp_path / "manifest.db")
    store = BarStore(tmp_path / "data", manifest=manifest)
    # Property access
    cov_prop = store.coverage_index
    assert isinstance(cov_prop, CoverageIndex)
    # Invocation with parentheses store.coverage_index() must return CoverageIndex
    cov_call = store.coverage_index()
    assert cov_call is cov_prop


# -----------------------------------------------------------------------------
# Step 3 & 8: code_hash includes plugin data files and ignores noise
# -----------------------------------------------------------------------------


def test_step3_code_hash_includes_plugin_data_files() -> None:
    """code_hash detects modifications to plugin data files (e.g. data/*.toml)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_plugin = Path(tmp_dir) / "exchange"
        shutil.copytree("plugins/calendars/exchange", tmp_plugin)

        h_before = code_hash(tmp_plugin)
        nse_file = tmp_plugin / "data" / "nse.toml"
        assert nse_file.exists()

        with open(nse_file, "a") as f:
            f.write("\n# modification for test\n")

        h_after = code_hash(tmp_plugin)
        assert h_before != h_after, "code_hash must change when a data file is modified"


def test_step3_code_hash_ignores_tests_pycache_readme_and_dotfiles() -> None:
    """code_hash ignores changes in tests/, __pycache__, README files, and dotfiles."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_plugin = Path(tmp_dir) / "exchange"
        shutil.copytree("plugins/calendars/exchange", tmp_plugin)

        h_baseline = code_hash(tmp_plugin)

        # 1. Edit README
        readme = tmp_plugin / "README.md"
        if readme.exists():
            with open(readme, "a") as f:
                f.write("\nNew doc line\n")
        assert code_hash(tmp_plugin) == h_baseline

        # 2. Add dotfile
        dotfile = tmp_plugin / ".DS_Store"
        dotfile.write_bytes(b"noise")
        assert code_hash(tmp_plugin) == h_baseline

        # 3. Add file in tests
        test_dir = tmp_plugin / "tests"
        test_dir.mkdir(exist_ok=True)
        (test_dir / "test_scratch.py").write_text("# test\n")
        assert code_hash(tmp_plugin) == h_baseline


# -----------------------------------------------------------------------------
# Step 6: Undeclared (source, timeframe) refused by BarStore & DataService
# -----------------------------------------------------------------------------


def test_step6_store_refuses_undeclared_timeframe_for_bhavcopy(tmp_path: Path) -> None:
    """BarStore.commit refuses 1m bars for nse_bhavcopy with actionable DataError."""
    manifest = DataManifest(tmp_path / "manifest.db")
    store = BarStore(tmp_path / "data", manifest=manifest)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1m", "raw", "nse_bhavcopy")
    bar = _make_bar(inst, datetime(2026, 11, 10, 9, 15, tzinfo=UTC), tf="1m")

    with pytest.raises(DataError) as exc_info:
        store.commit(key, [bar])

    err = exc_info.value
    assert "nse_bhavcopy" in err.what
    assert "1m" in err.what
    assert "BarStore.commit" in err.where
    assert "supported: ['1d']" in err.fix


def test_step6_data_service_refuses_undeclared_timeframe_for_bhavcopy(tmp_path: Path) -> None:
    """DataService.get refuses 1m timeframe for nse_bhavcopy before planning."""
    manifest = DataManifest(tmp_path / "manifest.db")
    store = BarStore(tmp_path / "data", manifest=manifest)
    ds = DataService(sources={}, default_source="nse_bhavcopy", store=store)
    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1m", "raw", "nse_bhavcopy")

    start = datetime(2026, 11, 10, 9, 15, tzinfo=UTC)
    end = datetime(2026, 11, 10, 15, 30, tzinfo=UTC)

    with pytest.raises(DataError) as exc_info:
        ds.get(key, start, end, source="nse_bhavcopy")

    err = exc_info.value
    assert "nse_bhavcopy" in err.what
    assert "1m" in err.what
    assert "DataService.get" in err.where
    assert "lacks explicit 'intervals' declaration" in err.what


def test_step6_bhavcopy_manifest_lacks_intervals_reported() -> None:
    """validate_source_timeframe reports when manifest lacks explicit intervals."""
    # Read actual nse_bhavcopy manifest from disk
    manifest_path = Path("plugins/references/nse_bhavcopy/plugin.toml")
    with open(manifest_path, "rb") as f:
        raw_toml = tomli.load(f)

    man = PluginManifest.model_validate(raw_toml)
    allowed = get_source_declared_intervals("nse_bhavcopy", manifest=man)
    assert allowed == ("1d",)

    with pytest.raises(DataError) as exc_info:
        validate_source_timeframe("nse_bhavcopy", "1m", manifest=man)

    assert "lacks explicit 'intervals' declaration" in exc_info.value.what


# -----------------------------------------------------------------------------
# Step 6: data verify lists bad rows, and data verify --fix quarantines them
# -----------------------------------------------------------------------------


def test_step6_data_verify_detects_and_quarantines_undeclared_parts(
    tmp_project: Path,
) -> None:
    """data verify flags undeclared timeframe parts, and --fix moves them to quarantine."""
    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    inst = InstrumentId("NSE:EQ:RELIANCE")

    # Create an illegal nse_bhavcopy 1m dataset and part
    ds_id = dataset_id_for_key(SeriesKey(inst, "bars", "1m", "raw", "nse_bhavcopy"))
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument=str(inst),
            kind="bars",
            timeframe="1m",
            adjustment="raw",
            source="nse_bhavcopy",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="none",
        )
    )

    dt1 = datetime(2026, 10, 26, 9, 15, tzinfo=UTC)
    dt2 = datetime(2026, 10, 26, 9, 16, tzinfo=UTC)
    b1 = _make_bar(inst, dt1, tf="1m", close=2500.0)
    b2 = _make_bar(inst, dt2, tf="1m", close=2505.0)

    p_file = bar_file_path(data_dir, SeriesKey(inst, "bars", "1m", "raw", "nse_bhavcopy"), dt1)
    write_bars_parquet(p_file, [b1, b2])

    rel_p = str(p_file.relative_to(data_dir))
    real_sha = _compute_sha256(p_file)
    manifest.record_part(
        PartRecord(
            path=rel_p,
            dataset_id=ds_id,
            rows=2,
            bytes=p_file.stat().st_size,
            first_ts=dt1.isoformat(),
            last_ts=dt2.isoformat(),
            sha256=real_sha,
            source="nse_bhavcopy",
            source_version="1.0",
            fetched_at=datetime.now(UTC).isoformat(),
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_id,
            day="2026-10-26",
            status=CoverageStatus.COMPLETE,
            rows=2,
            expected_rows=2,
            part_path=rel_p,
            fetched_at=datetime.now(UTC).isoformat(),
        )
    )
    manifest.close()

    runner = CliRunner()

    # 1. verify without --fix must report the rule violation and exit with 1
    res1 = runner.invoke(app, ["--root", str(tmp_project), "data", "verify"])
    assert res1.exit_code == 1
    assert "VERIFY: FAILED" in res1.output
    assert "undeclared timeframe '1m' for source 'nse_bhavcopy'" in res1.output

    # 2. verify with --fix must quarantine the offending part and record journal event
    res2 = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--fix"])
    assert res2.exit_code == 0
    assert "VERIFY: OK" in res2.output
    assert "Quarantined 2 offending bar(s)" in res2.output

    # File on disk must be removed
    assert not p_file.exists()

    # Part and dataset in manifest must be cleaned up
    m_check = DataManifest(data_dir / "manifest.db")
    assert m_check.get_parts(ds_id) == []
    assert m_check.get_dataset(ds_id) is None
    assert m_check.get_coverage_days(ds_id) == []
    m_check.close()

    # Verify journal recorded data.quarantine event
    j_events = list(events(tmp_project / "data" / "state" / "journal.db"))
    q_events = [e for e in j_events if e.get("kind") == "data.quarantine"]
    assert len(q_events) >= 1
    payload = json.loads(q_events[-1]["payload_json"])
    assert payload.get("action") == "quarantined"


# -----------------------------------------------------------------------------
# Step 5: data gaps unambiguous per-source blocks and checked series
# -----------------------------------------------------------------------------


def test_step5_data_gaps_prints_separate_block_per_source_without_source_flag(
    tmp_project: Path,
) -> None:
    """data gaps without --source prints one block per source with coverage."""
    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    inst = InstrumentId("NSE:EQ:TCS")

    # Source 1: upstox 1m
    ds_upstox = dataset_id_for_key(SeriesKey(inst, "bars", "1m", "raw", "upstox"))
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_upstox,
            instrument=str(inst),
            kind="bars",
            timeframe="1m",
            adjustment="raw",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_upstox,
            day="2026-10-26",
            status=CoverageStatus.COMPLETE,
            rows=375,
            expected_rows=375,
            part_path="part_upstox.parquet",
            fetched_at=datetime.now(UTC).isoformat(),
        )
    )

    # Source 2: yfinance 1m
    ds_yf = dataset_id_for_key(SeriesKey(inst, "bars", "1m", "raw", "yfinance"))
    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_yf,
            instrument=str(inst),
            kind="bars",
            timeframe="1m",
            adjustment="raw",
            source="yfinance",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_yf,
            day="2026-10-26",
            status=CoverageStatus.COMPLETE,
            rows=375,
            expected_rows=375,
            part_path="part_yf.parquet",
            fetched_at=datetime.now(UTC).isoformat(),
        )
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
            "--timeframe",
            "1m",
            "--from",
            "2026-10-26",
            "--to",
            "2026-10-27",
        ],
    )
    assert res.exit_code == 0
    # Both series must be checked and printed in separate blocks
    assert f"Checked series: {ds_upstox}" in res.output
    assert f"Checked series: {ds_yf}" in res.output


def test_step5_data_gaps_checks_only_specified_source(tmp_project: Path) -> None:
    """data gaps with --source evaluates and prints only that source."""
    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    inst = InstrumentId("NSE:EQ:INFY")

    ds_upstox = dataset_id_for_key(SeriesKey(inst, "bars", "1m", "raw", "upstox"))
    ds_yf = dataset_id_for_key(SeriesKey(inst, "bars", "1m", "raw", "yfinance"))

    for ds_id, src in ((ds_upstox, "upstox"), (ds_yf, "yfinance")):
        manifest.record_dataset(
            DatasetRecord(
                dataset_id=ds_id,
                instrument=str(inst),
                kind="bars",
                timeframe="1m",
                adjustment="raw",
                source=src,
                schema_version=1,
                created_at=datetime.now(UTC).isoformat(),
                actions_epoch=None,
                adjustment_basis="splits",
            )
        )
        manifest.record_coverage_day(
            CoverageDayRecord(
                dataset_id=ds_id,
                day="2026-10-26",
                status=CoverageStatus.COMPLETE,
                rows=375,
                expected_rows=375,
                part_path=f"part_{src}.parquet",
                fetched_at=datetime.now(UTC).isoformat(),
            )
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
            "INFY",
            "--timeframe",
            "1m",
            "--source",
            "upstox",
            "--from",
            "2026-10-26",
            "--to",
            "2026-10-27",
        ],
    )
    assert res.exit_code == 0
    assert f"Checked series: {ds_upstox}" in res.output
    assert ds_yf not in res.output


# -----------------------------------------------------------------------------
# Step 7: Core lines guidance budget
# -----------------------------------------------------------------------------


def test_step5_data_gaps_json_output(tmp_project: Path) -> None:
    """data gaps --json outputs valid JSON structure with per-gap source information."""
    data_dir = tmp_project / "data"
    manifest = DataManifest(data_dir / "manifest.db")
    inst = InstrumentId("NSE:EQ:SBIN")
    ds_upstox = dataset_id_for_key(SeriesKey(inst, "bars", "1m", "raw", "upstox"))

    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_upstox,
            instrument=str(inst),
            kind="bars",
            timeframe="1m",
            adjustment="raw",
            source="upstox",
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            actions_epoch=None,
            adjustment_basis="splits",
        )
    )
    manifest.record_coverage_day(
        CoverageDayRecord(
            dataset_id=ds_upstox,
            day="2026-10-26",
            status=CoverageStatus.COMPLETE,
            rows=375,
            expected_rows=375,
            part_path="part.parquet",
            fetched_at=datetime.now(UTC).isoformat(),
        )
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
            "SBIN",
            "--timeframe",
            "1m",
            "--from",
            "2026-10-26",
            "--to",
            "2026-10-28",
            "--json",
        ],
    )
    assert res.exit_code == 0
    parsed = json.loads(res.output)
    assert isinstance(parsed, list)
    assert len(parsed) >= 1
    assert parsed[0]["instrument"] == "NSE:EQ:SBIN"
    assert parsed[0]["source"] == "upstox"


def test_step7_core_lines_guidance_budget_is_30000() -> None:
    """config/params/core.toml sets budget.core_lines_guidance to 30000."""
    with open("config/params/core.toml", "rb") as f:
        data = tomli.load(f)

    params = data.get("param", [])
    guidance_param = next(
        (p for p in params if p.get("name") == "budget.core_lines_guidance"), None
    )
    assert guidance_param is not None
    assert guidance_param.get("default") == 30000
