"""Tests asserting config/screens/sma_trend_test.toml and plugins/screens/sma_trend_py.

Produce identical tables and result hashes.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest
from plugins.screens.sma_trend_py.plugin import SmaTrendPyPlugin
from tests.core.screens.conftest import (
    FakeDataReader,
    FakeIndicatorResolver,
    generate_daily_bars,
)

from algodesk.api.manifest import parse_manifest
from algodesk.results.screen_writer import write_screen_results
from algodesk.screens.models import load_toml_screen
from algodesk.screens.runner import ScreenRunner


def test_toml_equals_python_sma_trend(tmp_path: Path, fake_resolver: FakeIndicatorResolver) -> None:
    # 1. Load TOML screen
    toml_path = Path("config/screens/sma_trend_test.toml")
    assert toml_path.is_file(), f"Missing {toml_path}"
    toml_screen = load_toml_screen(toml_path)
    toml_file_hash = hashlib.sha256(toml_path.read_bytes()).hexdigest()

    # 2. Instantiate Python plugin
    plugin_toml = Path("plugins/screens/sma_trend_py/plugin.toml")
    assert plugin_toml.is_file(), f"Missing {plugin_toml}"
    import tomllib

    with open(plugin_toml, "rb") as f:
        data = tomllib.load(f)
    manifest = parse_manifest(data, str(plugin_toml))
    py_plugin = SmaTrendPyPlugin(manifest=manifest)

    # 3. Create synthetic universe with 5 symbols and 250 bars each
    # SYM1 & SYM2: upward trend (SMA50 > SMA200) -> PASS
    # SYM3 & SYM4: downward trend (SMA50 < SMA200) -> FAIL
    # SYM5: flat trend -> FAIL
    as_of = date(2026, 11, 12)
    syms = ["HDFCBANK", "RELIANCE", "INFY", "TCS", "SBIN"]
    reader = FakeDataReader()

    # Uptrend for HDFCBANK & RELIANCE (100 -> 300)
    uptrend = [100.0 + (i * 0.8) for i in range(250)]
    reader.add_bars("HDFCBANK", generate_daily_bars("HDFCBANK", uptrend, as_of=as_of))
    reader.add_bars("RELIANCE", generate_daily_bars("RELIANCE", uptrend, as_of=as_of))

    # Downtrend for INFY & TCS (300 -> 100)
    downtrend = [300.0 - (i * 0.8) for i in range(250)]
    reader.add_bars("INFY", generate_daily_bars("INFY", downtrend, as_of=as_of))
    reader.add_bars("TCS", generate_daily_bars("TCS", downtrend, as_of=as_of))

    # Flat for SBIN (100)
    flat = [100.0 for _ in range(250)]
    reader.add_bars("SBIN", generate_daily_bars("SBIN", flat, as_of=as_of))

    runner = ScreenRunner(reader, fake_resolver)

    # Execute both screens with identical parameters, flags, and as_of
    toml_result = runner.run(
        toml_screen,
        symbols=syms,
        as_of=as_of,
        adjust="total",
        screen_file_hash=toml_file_hash,
    )
    py_result = runner.run(
        py_plugin,
        symbols=syms,
        as_of=as_of,
        adjust="total",
        screen_file_hash=toml_file_hash,
    )

    # 4. Assert evaluations match identically
    assert len(toml_result.rows) == len(py_result.rows) == 5
    assert len(toml_result.skipped) == len(py_result.skipped) == 0

    toml_by_sym = {r.symbol: r for r in toml_result.rows}
    py_by_sym = {r.symbol: r for r in py_result.rows}

    for sym in syms:
        t_row = toml_by_sym[sym]
        p_row = py_by_sym[sym]
        assert t_row.passed == p_row.passed, f"Mismatch on {sym} passed"
        assert t_row.score == p_row.score, f"Mismatch on {sym} score"
        assert pytest.approx(t_row.values["sma_50"], rel=1e-5) == p_row.values["sma_50"]
        assert pytest.approx(t_row.values["sma_200"], rel=1e-5) == p_row.values["sma_200"]

    # 5. Assert result hashes are byte-identical
    assert toml_result.result_hash == py_result.result_hash

    # 6. Assert written artifact tables are identical
    dir_toml = write_screen_results(tmp_path / "results_toml", toml_result)
    dir_py = write_screen_results(tmp_path / "results_py", py_result)

    table_toml = (dir_toml / "table.csv").read_text(encoding="utf-8")
    table_py = (dir_py / "table.csv").read_text(encoding="utf-8")
    assert table_toml == table_py
