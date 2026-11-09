"""Tests for doctor check data.accumulation_lag."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from algodesk.api.checks import Severity
from algodesk.data.manifest import DataManifest
from algodesk.ops.checks import _check_data_accumulation_lag, _compute_instrument_lag


class DummyCal:
    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5


def test_compute_instrument_lag_up_to_date() -> None:
    cal = DummyCal()
    today = date(2026, 11, 4)
    latest_by_inst = {"NSE:EQ:RELIANCE": today}
    max_lag, lagging = _compute_instrument_lag(
        instruments=["NSE:EQ:RELIANCE"],
        latest_by_inst=latest_by_inst,
        latest_complete_date=today,
        cal=cal,
        warn_days=3,
    )
    assert max_lag == 0
    assert lagging is None


def test_compute_instrument_lag_detects_exceeding_threshold() -> None:
    cal = DummyCal()
    latest_complete = date(2026, 11, 10)  # Tuesday
    # Last stored is 10 calendar days earlier
    latest_by_inst = {"NSE:EQ:RELIANCE": date(2026, 10, 30)}
    max_lag, lagging = _compute_instrument_lag(
        instruments=["NSE:EQ:RELIANCE"],
        latest_by_inst=latest_by_inst,
        latest_complete_date=latest_complete,
        cal=cal,
        warn_days=3,
    )
    assert max_lag > 3
    assert lagging == "NSE:EQ:RELIANCE"


def test_doctor_accumulation_lag_skips_when_no_manifest(tmp_path: Path) -> None:
    class DummyPaths:
        data = tmp_path / "data"

    class DummyRuntime:
        root = tmp_path
        paths = DummyPaths()
        params = None

    (tmp_path / "data").mkdir(parents=True)
    res = _check_data_accumulation_lag(DummyRuntime())  # type: ignore[arg-type]
    assert res.ok is True
    assert "No data manifest found" in res.message


def test_doctor_accumulation_lag_warns_on_stale_store(tmp_project: Any) -> None:
    # On tmp_project without 1m bars recorded for demo5, doctor check warns
    from algodesk.ops.runtime import open_runtime

    with open_runtime(root=tmp_project, ensure=False) as rt:
        manifest_db = rt.paths.data / "state" / "data_manifest.db"
        manifest_db.parent.mkdir(parents=True, exist_ok=True)
        m = DataManifest(manifest_db)
        m.close()

        res = _check_data_accumulation_lag(rt)
        assert res.ok is False
        assert res.severity == Severity.WARNING
        assert "Accumulation lag" in res.message
        assert "algodesk data accumulate" in res.fix
