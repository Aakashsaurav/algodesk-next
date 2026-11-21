"""Tests for CP3: rebalances.csv, manifest screen hashes, and summary assumptions."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from algodesk.results.writer import ResultsWriter


def test_write_rebalances_csv_header_and_rows(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="test_run", results_dir=tmp_path)
    rebalances = [
        {
            "rebalance_date": "2024-01-02",
            "screen_result_hash": "hash_abc",
            "selected_symbols": "NSE:EQ:A:95.00;NSE:EQ:B:85.00",
            "intended_sold": "",
            "intended_bought": "NSE:EQ:A;NSE:EQ:B",
            "executed_sold": "",
            "executed_bought": "NSE:EQ:A;NSE:EQ:B",
            "rejected": "",
        },
        {
            "rebalance_date": "2024-02-01",
            "screen_result_hash": "hash_def",
            "selected_symbols": "NSE:EQ:A:96.00;NSE:EQ:C:88.00",
            "intended_sold": "NSE:EQ:B",
            "intended_bought": "NSE:EQ:C",
            "executed_sold": "NSE:EQ:B",
            "executed_bought": "NSE:EQ:C",
            "rejected": "",
        },
    ]

    writer._write_rebalances_csv(rebalances)

    csv_file = tmp_path / "test_run" / "rebalances.csv"
    assert csv_file.is_file()

    with open(csv_file, newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))

    assert len(reader) == 2
    assert reader[0]["rebalance_date"] == "2024-01-02"
    assert reader[0]["screen_result_hash"] == "hash_abc"
    assert reader[0]["selected_symbols"] == "NSE:EQ:A:95.00;NSE:EQ:B:85.00"
    assert reader[0]["intended_bought"] == "NSE:EQ:A;NSE:EQ:B"
    assert reader[0]["executed_bought"] == "NSE:EQ:A;NSE:EQ:B"
    assert reader[0]["intended_sold"] == ""
    assert reader[0]["executed_sold"] == ""
    assert reader[0]["rejected"] == ""

    assert reader[1]["rebalance_date"] == "2024-02-01"
    assert reader[1]["screen_result_hash"] == "hash_def"
    assert reader[1]["selected_symbols"] == "NSE:EQ:A:96.00;NSE:EQ:C:88.00"
    assert reader[1]["intended_bought"] == "NSE:EQ:C"
    assert reader[1]["executed_bought"] == "NSE:EQ:C"
    assert reader[1]["intended_sold"] == "NSE:EQ:B"
    assert reader[1]["executed_sold"] == "NSE:EQ:B"
    assert reader[1]["rejected"] == ""


def test_write_rebalances_csv_empty(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="empty_run", results_dir=tmp_path)
    writer._write_rebalances_csv([])

    csv_file = tmp_path / "empty_run" / "rebalances.csv"
    assert csv_file.is_file()

    with open(csv_file, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)

    assert header == [
        "rebalance_date",
        "screen_result_hash",
        "selected_symbols",
        "intended_sold",
        "intended_bought",
        "executed_sold",
        "executed_bought",
        "rejected",
    ]
    assert rows == []


def test_manifest_covers_screen_result_hashes(tmp_path: Path) -> None:
    writer1 = ResultsWriter(run_id="run_hash1", results_dir=tmp_path)
    writer2 = ResultsWriter(run_id="run_hash2", results_dir=tmp_path)

    manifest_info1: dict[str, Any] = {
        "mode": "backtest",
        "fidelity": "bar",
        "screen_result_hashes": ["hash_1", "hash_2"],
    }
    manifest_info2: dict[str, Any] = {
        "mode": "backtest",
        "fidelity": "bar",
        "screen_result_hashes": ["hash_1", "hash_DIFF"],
    }

    metrics: dict[str, Any] = {"trades": 5, "net_pnl": 1000.0}
    reg_snap: dict[str, Any] = {"params": {}}

    writer1._update_backtest_manifest(
        manifest=manifest_info1,
        metrics=metrics,
        trades=[],
        effective_config_text="strat = test",
        registry_snapshot=reg_snap,
        readiness_data=None,
    )
    writer2._update_backtest_manifest(
        manifest=manifest_info2,
        metrics=metrics,
        trades=[],
        effective_config_text="strat = test",
        registry_snapshot=reg_snap,
        readiness_data=None,
    )

    assert "result_hash" in manifest_info1
    assert "result_hash" in manifest_info2
    assert manifest_info1["screen_result_hashes"] == ["hash_1", "hash_2"]
    # Result hash must be different because screen_result_hashes differ
    assert manifest_info1["result_hash"] != manifest_info2["result_hash"]


def test_summary_md_adds_screen_assumptions_line(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="run_sum", results_dir=tmp_path)
    summary_info = {
        "run_name": "rotation_run",
        "mode": "backtest",
        "fidelity": "bar",
        "date": "2024-01-02",
        "verdict": "PASS",
        "screen_name": "trend_rank_test",
        "screen_hash": "sha256_screen_hash",
    }
    metrics = {
        "trades": 10,
        "net_pnl": 5000.0,
        "gross_pnl": 5200.0,
        "charges": 200.0,
        "win_rate": 60.0,
        "max_drawdown": 4.5,
        "closed_round_trips": 5,
    }

    writer._write_summary(summary_info, metrics, readiness_data=None)

    sum_file = tmp_path / "run_sum" / "SUMMARY.md"
    assert sum_file.is_file()
    content = sum_file.read_text(encoding="utf-8")

    assert (
        "screen trend_rank_test (sha256_screen_hash), current index list used (survivorship)"
        in content
    )

    # Word count check
    words = content.split()
    assert len(words) <= 150, f"Word count {len(words)} exceeds 150 words budget"


def test_rebalances_csv_not_written_in_paper_mode(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="run_paper", results_dir=tmp_path)
    summary_info = {
        "run_name": "paper_test",
        "mode": "paper",
        "fidelity": "tick",
        "date": "2024-01-02",
        "verdict": "PASS",
    }
    rebalances = [
        {
            "rebalance_date": "2024-01-02",
            "screen_result_hash": "hash_paper",
            "selected_symbols": "SYM1:100.00",
            "bought": "SYM1",
            "sold": "",
        }
    ]
    writer.write(
        summary_info=summary_info,
        metrics={},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
        rebalances=rebalances,
    )
    reb_csv = tmp_path / "run_paper" / "rebalances.csv"
    assert not reb_csv.exists(), "rebalances.csv must not be written in paper mode"


def test_rebalances_csv_not_written_in_live_mode(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="run_live", results_dir=tmp_path)
    summary_info = {
        "run_name": "live_test",
        "mode": "live",
        "fidelity": "tick",
        "date": "2024-01-02",
        "verdict": "PASS",
    }
    rebalances = [
        {
            "rebalance_date": "2024-01-02",
            "screen_result_hash": "hash_live",
            "selected_symbols": "SYM1:100.00",
            "bought": "SYM1",
            "sold": "",
        }
    ]
    writer.write(
        summary_info=summary_info,
        metrics={},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
        rebalances=rebalances,
    )
    reb_csv = tmp_path / "run_live" / "rebalances.csv"
    assert not reb_csv.exists(), "rebalances.csv must not be written in live mode"


def test_manifest_without_screen_result_hashes_defaults_to_empty(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="run_no_screen", results_dir=tmp_path)
    manifest: dict[str, Any] = {}
    reg_snap = {"hash": "core", "params": {}}
    writer._update_backtest_manifest(
        manifest=manifest,
        metrics={},
        trades=[],
        effective_config_text="strat = foo",
        registry_snapshot=reg_snap,
        readiness_data=None,
    )
    assert manifest["screen_result_hashes"] == []
    assert isinstance(manifest["result_hash"], str)
    assert len(manifest["result_hash"]) == 64


def test_summary_assumptions_without_screen_omits_screen_clause(tmp_path: Path) -> None:
    writer = ResultsWriter(run_id="run_no_scr_sum", results_dir=tmp_path)
    summary_info = {
        "run_name": "no_scr_run",
        "mode": "backtest",
        "fidelity": "bar",
        "date": "2024-01-02",
        "verdict": "PASS",
    }
    metrics = {
        "trades": 0,
        "net_pnl": 0.0,
        "gross_pnl": 0.0,
        "charges": 0.0,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
        "closed_round_trips": 0,
    }
    writer._write_summary(summary_info, metrics, readiness_data=None)
    content = (tmp_path / "run_no_scr_sum" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "screen" not in content
    assert "survivorship note (point-in-time universe unadjusted)." in content
