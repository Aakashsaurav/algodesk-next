"""Tests for results bundle output, results list compatibility, and 500-symbol benchmark."""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from tests.core.screens.conftest import (
    FakeDataReader,
    FakeIndicatorResolver,
    generate_daily_bars,
)

from algodesk.api.screens import ScreenResult, ScreenRow, SkipReason
from algodesk.results.screen_writer import write_screen_results
from algodesk.screens.models import (
    OperandModel,
    RequireRule,
    TomlScreenModel,
)
from algodesk.screens.runner import ScreenRunner


def test_write_screen_results_artifacts(tmp_path: Path) -> None:
    result = ScreenResult(
        name="test_screen",
        as_of=date(2026, 11, 12),
        timeframe="1d",
        rows=[
            ScreenRow(symbol="SYM1", passed=True, score=90.0, values={"metric": 10.5}),
            ScreenRow(symbol="SYM2", passed=True, score=95.0, values={"metric": 12.0}),
            ScreenRow(symbol="SYM3", passed=False, score=10.0, values={"metric": 1.0}),
        ],
        skipped=[
            ("SYM4", SkipReason.NO_DATA, "No bars found"),
            ("SYM5", SkipReason.INSUFFICIENT_HISTORY, "Short history"),
        ],
        universe_hash="univ_hash_123",
        result_hash="res_hash_456",
        provenance={"SYM1": {"checksums": ["c1"]}},
        parameters={"p1": 1},
        flags={"adjust": "total", "accept_unverified": False},
        verdict="PASS",
        run_id="screen_test_run_1",
    )

    results_dir = tmp_path / "results"
    run_dir = write_screen_results(results_dir, result, effective_config_text="# Config\n")

    assert run_dir.is_dir()
    summary_path = run_dir / "SUMMARY.md"
    assert summary_path.is_file()
    summary_text = summary_path.read_text(encoding="utf-8")
    assert len(summary_text.split()) <= 150
    assert "Verdict: PASS." in summary_text
    assert "Date: 2026-11-12" in summary_text
    assert "Mode: screen" in summary_text

    table_path = run_dir / "table.csv"
    assert table_path.is_file()
    table_lines = table_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(table_lines) == 3  # Header + 2 passed rows
    # SYM2 (score 95) should be first, then SYM1 (score 90)
    assert "SYM2" in table_lines[1]
    assert "SYM1" in table_lines[2]

    skipped_path = run_dir / "skipped.csv"
    assert skipped_path.is_file()
    skipped_lines = skipped_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(skipped_lines) == 3  # Header + 2 skipped rows

    manifest_path = run_dir / "manifest.json"
    assert manifest_path.is_file()
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest_data["run_id"] == "screen_test_run_1"
    assert manifest_data["run_type"] == "screen"
    assert manifest_data["result_hash"] == "res_hash_456"
    assert manifest_data["stats"]["passed"] == 2
    assert manifest_data["stats"]["skipped"] == 2

    metrics_path = run_dir / "metrics.json"
    assert metrics_path.is_file()
    metrics_data = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics_data["mode"] == "screen"


def test_500_symbol_benchmark(fake_resolver: FakeIndicatorResolver) -> None:
    """Benchmark: 500 symbols with 250 daily bars evaluated through ScreenRunner.

    Reported, not gated.
    """
    as_of = date(2026, 11, 12)
    num_symbols = 500
    num_bars = 250

    reader = FakeDataReader()
    # Pre-generate prices once and duplicate bars across symbols with unique symbol names
    base_prices = [100.0 + (i % 50) for i in range(num_bars)]
    sym_list = [f"SYM_{i:04d}" for i in range(num_symbols)]

    for sym in sym_list:
        bars = generate_daily_bars(sym, base_prices, as_of=as_of)
        reader.add_bars(sym, bars)

    runner = ScreenRunner(reader, fake_resolver)
    screen = TomlScreenModel(
        name="benchmark_screen",
        require=[
            RequireRule(
                left=OperandModel(ind="sma", n=50),
                op=">",
                right=OperandModel(ind="sma", n=200),
            )
        ],
    )

    t0 = time.perf_counter()
    result = runner.run(screen, symbols=sym_list, as_of=as_of)
    elapsed = time.perf_counter() - t0

    assert len(result.rows) == num_symbols
    assert len(result.skipped) == 0
    # Report benchmark timing in test output
    print(f"\n[BENCHMARK] 500 symbols x 250 bars screened in {elapsed:.2f} s")
