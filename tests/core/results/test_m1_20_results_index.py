"""Tests for M1-20 CP1: automatic results indexing and results list sync."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from algodesk.api.screens import ScreenResult
from algodesk.cli.app import app
from algodesk.results.index import ResultsIndex
from algodesk.results.screen_writer import write_screen_results
from algodesk.results.writer import ResultsWriter

runner = CliRunner()


def _write_dummy_bundle(
    results_dir: Path,
    run_id: str,
    mode: str = "paper",
    verdict: str = "PASS",
    stop_reason: str = "end of session",
    trades: int = 2,
    net_pnl: float = 100.0,
) -> Path:
    writer = ResultsWriter(run_id, results_dir=results_dir)
    return writer.write(
        summary_info={
            "run_name": "demo_paper_fast",
            "mode": mode,
            "date": "2026-11-25",
            "verdict": verdict,
            "stop_reason": stop_reason,
        },
        metrics={
            "mode": mode,
            "trades": trades,
            "net_pnl": net_pnl,
            "win_rate": 50.0,
            "max_drawdown": 1.0,
            "first_tick_ist": "2026-11-25 09:15:00 IST",
            "last_tick_ist": "2026-11-25 15:30:00 IST",
        },
        trades=[],
        equity_points=[],
        effective_config_text="mode = 'paper'\n",
        registry_snapshot={},
        manifest_info={"mode": mode, "verdict": verdict, "stop_reason": stop_reason},
    )


def test_paper_then_results_list(tmp_project: Path) -> None:
    """Paper run finalisation writes results_index.db and appears in results list."""
    results_dir = tmp_project / "results"
    _write_dummy_bundle(results_dir, "demo_paper_fast_paper_20261125_070106_77666d", mode="paper")

    res = runner.invoke(app, ["--root", str(tmp_project), "results", "list", "--type", "paper"])
    assert res.exit_code == 0, res.output
    assert "demo_paper_fast_paper_20261125_070106_77666d" in res.output
    assert "indexed" not in res.output


def test_replay_then_results_list(tmp_project: Path) -> None:
    """Replay run finalisation writes results_index.db and appears in results list."""
    results_dir = tmp_project / "results"
    _write_dummy_bundle(results_dir, "demo_paper_fast_replay_20261125_105627_187c0d", mode="replay")

    res = runner.invoke(app, ["--root", str(tmp_project), "results", "list", "--type", "replay"])
    assert res.exit_code == 0, res.output
    assert "demo_paper_fast_replay_20261125_105627_187c0d" in res.output
    assert "indexed" not in res.output


def test_screen_then_results_list(tmp_project: Path) -> None:
    """Screen run finalisation writes results_index.db and appears in results list."""
    results_dir = tmp_project / "results"
    sr = ScreenResult(
        run_id="screen_trend_20261125_090000",
        name="trend",
        as_of=date(2026, 11, 25),
        timeframe="1d",
        rows=[],
        skipped=[],
        verdict="PASS",
        result_hash="abc123",
        universe_hash="def456",
    )
    write_screen_results(results_dir, sr, effective_config_text="name = 'trend'\n")

    res = runner.invoke(app, ["--root", str(tmp_project), "results", "list", "--type", "screen"])
    assert res.exit_code == 0, res.output
    assert "screen_trend_20261125_090000" in res.output


def test_interrupted_and_error_runs_indexed(tmp_project: Path) -> None:
    """Interrupted and error runs are indexed on finalisation."""
    results_dir = tmp_project / "results"
    _write_dummy_bundle(
        results_dir,
        "demo_paper_fast_paper_20261125_080000_aaaaaa",
        mode="paper",
        verdict="STOPPED",
        stop_reason="interrupt",
    )
    _write_dummy_bundle(
        results_dir,
        "demo_paper_fast_paper_20261125_090000_bbbbbb",
        mode="paper",
        verdict="FAIL",
        stop_reason="error",
    )

    db_path = tmp_project / "data" / "state" / "results_index.db"
    idx = ResultsIndex(db_path)
    runs = {r["run_id"]: r for r in idx.list_runs(run_type="paper")}
    assert runs["demo_paper_fast_paper_20261125_080000_aaaaaa"]["verdict"] == "STOPPED"
    assert runs["demo_paper_fast_paper_20261125_090000_bbbbbb"]["verdict"] == "FAIL"


def test_results_list_scans_missing_and_limits(tmp_project: Path) -> None:
    """results list indexes missing finished runs and respects --limit newest first."""
    results_dir = tmp_project / "results"
    _write_dummy_bundle(results_dir, "alpha_paper_20261120_090000_111111", mode="paper")

    # Create a run on disk bypassing the index (delete its row from DB)
    _write_dummy_bundle(results_dir, "beta_paper_20261125_100000_222222", mode="paper")
    db_path = tmp_project / "data" / "state" / "results_index.db"
    idx = ResultsIndex(db_path)
    assert idx.delete_run("beta_paper_20261125_100000_222222")

    res = runner.invoke(app, ["--root", str(tmp_project), "results", "list", "--limit", "1"])
    assert res.exit_code == 0, res.output
    assert "indexed 1 new runs" in res.output
    assert "beta_paper_20261125_100000_222222" in res.output
    assert "alpha_paper_20261120_090000_111111" not in res.output


def test_corrupt_folder_skipped_with_warning(
    tmp_project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt results folder is skipped with a warning and does not break results list."""
    results_dir = tmp_project / "results"
    _write_dummy_bundle(results_dir, "good_paper_20261125_090000_111111", mode="paper")

    corrupt_dir = results_dir / "corrupt_paper_20261125_100000_badbad"
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    (corrupt_dir / "metrics.json").write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        res = runner.invoke(app, ["--root", str(tmp_project), "results", "list"])
    assert res.exit_code == 0, res.output
    assert "good_paper_20261125_090000_111111" in res.output
    assert "corrupt_paper_20261125_100000_badbad" not in res.output
    assert any("Skipping corrupt results folder" in r.message for r in caplog.records)
