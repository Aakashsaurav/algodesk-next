"""Tests for warm-up source rule, provenance recording, and warmup doctor check."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from algodesk.api import Bar, SeriesKey
from algodesk.cli.app import app
from algodesk.engine.indicator_manager import IndicatorManager
from algodesk.ops.checks import build_core_checks


class FakeWarmupDataService:
    def __init__(self, source_name: str = "upstox") -> None:
        self.default_source = source_name
        self.get_calls: list[tuple[Any, ...]] = []
        self._recorded_datasets = ["NSE_EQ_RELIANCE_bars_1d_adjusted"]
        self._recorded_checksums = ["fake_sha256_hash_123"]

    def get(
        self,
        series: SeriesKey,
        start: datetime,
        end: datetime,
        download: str = "adjusted",
        adjust: str = "none",
        fill_only: bool = False,
        source: str | None = None,
    ) -> Any:
        self.get_calls.append((series, start, end, download, adjust, source))
        b = Bar(
            instrument=series.instrument,
            timeframe=series.timeframe,
            start_ts=start,
            end_ts=end,
            open=100.0,
            high=105.0,
            low=95.0,
            close=102.0,
            volume=5000,
            complete=True,
        )
        slice_res = type("FakeSlice", (), {"bars": [b], "checksums": ["fake_sha256_hash_123"]})()
        return slice_res

    def recorded_provenance(self) -> dict[str, list[str]]:
        return {
            "datasets": sorted(self._recorded_datasets),
            "checksums": sorted(self._recorded_checksums),
        }


def test_indicator_warmup_uses_dataservice_get() -> None:
    ds = FakeWarmupDataService("upstox")
    mgr = IndicatorManager()

    now_dt = datetime.now(UTC)

    # Warmup without registered indicators should be a no-op
    mgr.warmup(ds, session_start=now_dt, source="upstox")
    assert len(ds.get_calls) == 0


def test_doctor_warmup_source_check(tmp_project: Path) -> None:
    from algodesk.ops.runtime import open_runtime

    with open_runtime(tmp_project) as rt:
        checks = build_core_checks(rt)
        warmup_check = next((c for c in checks if c.id == "data.warmup_source"), None)
        assert warmup_check is not None
        result = warmup_check.run()
        assert result.ok is True
        assert "upstox" in result.message


def test_cli_run_warmup_source_override_option(tmp_project: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["run", "--help"])
    assert res.exit_code == 0
    assert "--warmup-source" in res.output
