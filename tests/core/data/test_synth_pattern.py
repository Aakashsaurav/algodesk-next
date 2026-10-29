"""Tests for synthetic data pattern (random vs trend) and SMA crossover trades."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from algodesk.config.snapshot import build_effective
from algodesk.data.manifest import DataManifest
from algodesk.data.synth import generate_synthetic_day, synth_ticks
from algodesk.engine.runner import SessionRunner


def test_synthetic_day_invalid_pattern() -> None:
    d = date(2026, 10, 26)
    with pytest.raises(ValueError, match="Unknown synthetic data pattern"):
        generate_synthetic_day("NSE:EQ:RELIANCE", d, rows=10, seed=1, pattern="invalid")


def test_synthetic_day_trend_determinism() -> None:
    d = date(2026, 10, 26)
    evs_1 = generate_synthetic_day("NSE:EQ:RELIANCE", d, rows=100, seed=42, pattern="trend")
    evs_2 = generate_synthetic_day("NSE:EQ:RELIANCE", d, rows=100, seed=42, pattern="trend")
    assert len(evs_1) == 100
    for e1, e2 in zip(evs_1, evs_2, strict=True):
        assert e1.ltp == e2.ltp
        assert e1.exch_ts == e2.exch_ts


def test_synth_ticks_trend_generates_sma_crossovers_and_trades(tmp_project: Path) -> None:
    """Verify that trend pattern produces oscillating prices that create SMA crossovers."""
    data_dir = tmp_project / "data"
    manifest_db = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(manifest_db)

    # Generate trend data for RELIANCE and TCS
    symbols = ["NSE:EQ:RELIANCE", "NSE:EQ:TCS"]
    synth_ticks(
        symbols=symbols,
        days=2,
        rows_per_day=4000,
        seed=3,
        data_dir=data_dir,
        manifest=manifest,
        pattern="trend",
    )

    from algodesk.ops.runtime import open_runtime

    with open_runtime(tmp_project, run="demo_paper", ensure=True) as rt:
        eff = build_effective(rt.root, run_name="demo_paper")
        runner = SessionRunner(
            rt,
            run_config=eff.run,
            mode="replay",
            replay_dates=["2026-10-26"],
            run_id="test_trend_replay",
        )
        res_path = asyncio.run(runner.run())
    assert res_path.is_dir()
    trades_file = res_path / "trades.csv"
    assert trades_file.is_file()

    import json

    metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["trades"] >= 2, f"Expected >= 2 trades, got {metrics['trades']}"
