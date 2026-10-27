"""Tests for deterministic synthetic market data generator."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from algodesk.api.market import Quote, Tick
from algodesk.data.manifest import DataManifest
from algodesk.data.synth import generate_synthetic_day, synth_ticks


def test_synthetic_day_determinism() -> None:
    d = date(2026, 10, 26)
    events1 = generate_synthetic_day("NSE:EQ:INFY", d, rows=50, seed=42)
    events2 = generate_synthetic_day("NSE:EQ:INFY", d, rows=50, seed=42)

    assert len(events1) == 50
    assert len(events2) == 50

    for e1, e2 in zip(events1, events2, strict=True):
        assert e1.exch_ts == e2.exch_ts
        assert e1.ltp == e2.ltp
        assert e1.volume == e2.volume


def test_synthetic_day_quotes_and_depth() -> None:
    d = date(2026, 10, 26)
    events = generate_synthetic_day("NSE:EQ:SBIN", d, rows=20, seed=123)

    quotes = [e for e in events if isinstance(e, Quote)]
    ticks = [e for e in events if isinstance(e, Tick)]

    assert len(quotes) > 0
    assert len(ticks) > 0

    for q in quotes:
        assert len(q.bids) == 5
        assert len(q.asks) == 5
        assert q.bids[0].price < q.asks[0].price
        # Verify bid prices descending, ask prices ascending
        for i in range(len(q.bids) - 1):
            assert q.bids[i].price > q.bids[i + 1].price
        for i in range(len(q.asks) - 1):
            assert q.asks[i].price < q.asks[i + 1].price


def test_synth_ticks_manifest_registration(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manifest_db = tmp_path / "state" / "manifest.db"
    manifest = DataManifest(manifest_db)

    symbols = ["NSE:EQ:HDFCBANK", "NSE:EQ:RELIANCE"]
    total = synth_ticks(
        symbols=symbols,
        days=2,
        rows_per_day=30,
        seed=1,
        data_dir=data_dir,
        manifest=manifest,
    )

    assert total == 120  # 2 symbols * 2 days * 30 rows
    files = manifest.get_files()
    assert len(files) == 4
    for f in files:
        assert f.rows == 30
        assert f.source == "synthetic"
        assert f.state == "day"
        assert Path(f.path).is_file()

    insts = manifest.list_instruments()
    assert len(insts) == 2
