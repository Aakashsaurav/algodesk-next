"""Tests for Step 10 (readiness without delete advice) and Step 11 (assumptions line)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from algodesk.api.market import Bar, InstrumentId, SeriesKey
from algodesk.api.plugin import PluginManifest
from algodesk.api.protocols import DataSource
from algodesk.data.manifest import DataManifest
from algodesk.data.readiness import check_backtest_readiness
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.results.writer import ResultsWriter


class FakeDataSource(DataSource):
    def __init__(self, name: str = "upstox") -> None:
        self.manifest = PluginManifest(
            name=name,
            version="1.0.0",
            kind="broker",
            api_version=1,
            entry=f"plugins.brokers.{name}.broker:Broker",
            capabilities={"history": True},
            retention={"1d": 36500},
            max_span={"1d": 3650},
        )

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        return []

    def instruments(self) -> Iterable[Any]:
        return []


class SimpleCalendar:
    def is_session(self, dt: date | datetime) -> bool:
        d = dt if isinstance(dt, date) and not isinstance(dt, datetime) else dt.date()
        return d.weekday() < 5


def _commit_bars(
    store: BarStore,
    manifest: DataManifest,
    symbol: str,
    days: list[date],
    adjustment: str = "raw",
    source: str = "upstox",
) -> None:
    bars = []
    inst = InstrumentId(symbol)
    for d in days:
        s_dt = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)
        e_dt = datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC)
        b = Bar(
            instrument=inst,
            timeframe="1d",
            start_ts=s_dt,
            end_ts=e_dt,
            open=1260.0,
            high=1270.0,
            low=1255.0,
            close=1265.0,
            volume=50000,
            complete=True,
        )
        bars.append(b)

    key = SeriesKey(inst, "bars", "1d", adjustment, source)
    store.commit(key, bars)


def test_readiness_alternate_partitions_single_info_line_no_delete(tmp_path: Path) -> None:
    """Step 10: Alternate partitions produce 1 info line per symbol and no delete advice."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    source = FakeDataSource("upstox")
    service = DataService(sources={"upstox": source}, default_source="upstox", store=store)
    cal = SimpleCalendar()

    days = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]
    # Primary raw series
    _commit_bars(store, manifest, "NSE:EQ:RELIANCE", days, adjustment="raw")
    # Alternate adjusted series
    _commit_bars(store, manifest, "NSE:EQ:RELIANCE", days[:1], adjustment="adjusted")

    rep = check_backtest_readiness(
        symbols=["NSE:EQ:RELIANCE"],
        start=date(2025, 1, 6),
        end=date(2025, 1, 8),
        data_service=service,
        manifest=manifest,
        calendar=cal,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
        adjust="splits",
    )

    # Symbol is ready (healthy raw series exists)
    assert "NSE:EQ:RELIANCE" in rep.with_data
    sym_rep = rep.symbol_reports["NSE:EQ:RELIANCE"]

    # Alternate partition produces exactly ONE info warning
    assert len(sym_rep.warnings) == 1
    assert sym_rep.warnings[0] == "1 other series exist, not used by this run"

    # No cleanup commands anywhere
    assert len(sym_rep.warning_cleanup_commands) == 0
    assert len(rep.remediation_commands) == 0

    # JSON output must not contain "data delete"
    rep_dict = rep.to_dict()
    json_str = json.dumps(rep_dict)
    assert "data delete" not in json_str


def test_readiness_coverage_without_bars_produces_repair_fix(tmp_path: Path) -> None:
    """Step 10: Primary-series coverage disagreement prints coverage repair fix."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    source = FakeDataSource("upstox")
    service = DataService(sources={"upstox": source}, default_source="upstox", store=store)
    cal = SimpleCalendar()

    inst = InstrumentId("NSE:EQ:RELIANCE")
    key = SeriesKey(inst, "bars", "1d", "raw", "upstox")
    from algodesk.data.layout import dataset_id_for_key
    from algodesk.data.manifest import CoverageDayRecord

    ds_id = dataset_id_for_key(key)
    cov = CoverageDayRecord(
        dataset_id=ds_id,
        day="2025-01-06",
        status="COMPLETE",
        rows=1,
        expected_rows=1,
        part_path="part.parquet",
        fetched_at="2025-01-06T16:00:00Z",
        note="",
    )
    manifest.record_coverage_days([cov])

    rep = check_backtest_readiness(
        symbols=["NSE:EQ:RELIANCE"],
        start=date(2025, 1, 6),
        end=date(2025, 1, 6),
        data_service=service,
        manifest=manifest,
        calendar=cal,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
        adjust="splits",
    )

    sym_rep = rep.symbol_reports["NSE:EQ:RELIANCE"]
    assert not sym_rep.is_ready
    assert "coverage_without_bars" == sym_rep.primary_skip_reason

    # Fix command must be coverage repair, never delete
    assert len(sym_rep.fix_commands) >= 1
    assert "algodesk data coverage repair" in sym_rep.fix_commands[0]
    assert "data delete" not in str(sym_rep.fix_commands)


def test_readiness_missing_session_produces_accumulate_fix(tmp_path: Path) -> None:
    """Step 10: Missing primary data prints accumulate fix, never delete."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(data_dir / "manifest.db")
    store = BarStore(data_dir=data_dir, manifest=manifest)
    source = FakeDataSource("upstox")
    service = DataService(sources={"upstox": source}, default_source="upstox", store=store)
    cal = SimpleCalendar()

    # Missing session on 2025-01-06
    rep = check_backtest_readiness(
        symbols=["NSE:EQ:RELIANCE"],
        start=date(2025, 1, 6),
        end=date(2025, 1, 6),
        data_service=service,
        manifest=manifest,
        calendar=cal,
        timeframe="1d",
        source="upstox",
        warmup_bars_count=0,
        adjust="splits",
    )

    sym_rep = rep.symbol_reports["NSE:EQ:RELIANCE"]
    assert not sym_rep.is_ready
    assert "missing_sessions" == sym_rep.primary_skip_reason
    assert "algodesk data accumulate" in sym_rep.fix_commands[0]
    assert "data delete" not in str(sym_rep.fix_commands)


def test_summary_assumptions_real_fee_schedule_and_verified_on(tmp_path: Path) -> None:
    """Step 11: SUMMARY.md assumptions line uses real fee schedule and verified_on."""
    results_dir = tmp_path / "results"
    writer = ResultsWriter(run_id="assumptions_check_run", results_dir=results_dir)

    summary_info = {
        "run_name": "sma_1d_bt",
        "mode": "backtest",
        "date": "2026-11-19",
        "verdict": "PASS",
        "fee_schedule": "zerodha_nse",
    }
    metrics = {
        "trades": 150,
        "net_pnl": 54200.50,
        "gross_pnl": 58000.00,
        "charges": 3799.50,
        "win_rate": 56.5,
        "max_drawdown": 4.25,
    }
    effective_cfg = '[fees]\nschedule = "zerodha_nse"\n'

    writer.write(
        summary_info=summary_info,
        metrics=metrics,
        trades=[],
        equity_points=[],
        effective_config_text=effective_cfg,
        registry_snapshot={"hash": "core", "params": {}},
        manifest_info={"mode": "backtest", "code_version": "0.1.0"},
    )

    summary_md = results_dir / "assumptions_check_run" / "SUMMARY.md"
    assert summary_md.is_file()
    text = summary_md.read_text(encoding="utf-8")

    # Word limit <= 150 words
    words = text.split()
    assert len(words) <= 150

    # Real fee schedule name from config
    assert "fee schedule (zerodha_nse" in text
    # Verified on clause from schedule data
    assert (
        "rates verified on 2026-11-20 applied to all dates; " in text
        or "rates verified on 2026-11-02 applied to all dates; " in text
    )
    assert "effective_from before that is declared, not independently verified" in text
    assert "zerodha_eq" not in text
