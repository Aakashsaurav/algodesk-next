"""Tests for Steps 9 & 10: Results index (SQLite) and CLI commands.

Covers index rebuild, list, show, delete, and compare.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from algodesk.cli.app import app
from algodesk.results.compare import compare_backtest_manifests, format_backtest_compare_table
from algodesk.results.index import ResultsIndex


def _create_mock_results_folder(
    results_dir: Path,
    run_id: str,
    *,
    mode: str = "backtest",
    study: str | None = None,
    net_pnl: float = 1500.0,
    trades: int = 40,
    result_hash: str = "hash_123",
) -> Path:
    run_dir = results_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    metrics = {
        "mode": mode,
        "net_pnl": net_pnl,
        "gross_pnl": net_pnl + 50.0,
        "charges": 50.0,
        "trades": trades,
        "win_rate": 60.0,
        "max_drawdown": 2.5,
        "cagr": 0.15,
        "sharpe": 1.5,
        "sortino": 2.0,
        "first_tick_ist": "2025-01-01 09:15:00 IST",
        "last_tick_ist": "2025-06-01 15:30:00 IST",
        "date": "2025-01-01",
    }
    if study:
        metrics["study"] = study
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")

    manifest = {
        "mode": mode,
        "code_version": "0.1.0",
        "result_hash": result_hash,
        "seed": 42,
        "dataset_checksums": ["chk1", "chk2"],
        "fee_schedule_hash": "fee_h1",
        "strategy_parameters": {"fast": 10},
    }
    if study:
        manifest["study"] = study
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    summary_text = (
        f"# Run Summary: {run_id}\n\n"
        "## Summary\n"
        f"AlgoDesk Next {mode} session '{run_id}' executed on 2025-01-01 IST. "
        f"Net P&L: ₹{net_pnl:.2f} across 20 round trips ({trades} trades). "
        "Verdict: PASS.\n"
    )
    (run_dir / "SUMMARY.md").write_text(summary_text, encoding="utf-8")
    (run_dir / "config.effective.txt").write_text("strategy = 'sma_cross'\n", encoding="utf-8")

    return run_dir


def test_results_index_rebuild_and_query(tmp_path: Path) -> None:
    """ResultsIndex rebuilds index from disk and supports filtering."""
    results_dir = tmp_path / "results"
    db_path = tmp_path / "data" / "state" / "results_index.db"

    _create_mock_results_folder(results_dir, "run_bt_1", mode="backtest", study="sweep_a")
    _create_mock_results_folder(results_dir, "run_bt_2", mode="backtest", study="sweep_b")
    _create_mock_results_folder(results_dir, "run_paper_1", mode="paper")

    index = ResultsIndex(db_path)
    count = index.rebuild(results_dir)
    assert count == 3

    # Query all
    all_runs = index.list_runs()
    assert len(all_runs) == 3

    # Filter by type
    bt_runs = index.list_runs(run_type="backtest")
    assert len(bt_runs) == 2
    paper_runs = index.list_runs(run_type="paper")
    assert len(paper_runs) == 1

    # Filter by study
    study_a = index.list_runs(study="sweep_a")
    assert len(study_a) == 1
    assert study_a[0]["run_id"] == "run_bt_1"

    # Count study trials
    assert index.count_study_trials("sweep_a") == 1
    assert index.count_study_trials("sweep_b") == 1
    assert index.count_study_trials("sweep_c") == 0


def test_cli_results_index_rebuild(tmp_project: Path) -> None:
    """'algodesk results index rebuild' command indexes past runs."""
    results_dir = tmp_project / "results"
    _create_mock_results_folder(results_dir, "run_idx_1", mode="backtest")

    runner = CliRunner()
    res = runner.invoke(app, ["results", "index", "rebuild"])
    assert res.exit_code == 0
    assert "Rebuilt results index:" in res.stdout
    assert "1 run(s) indexed" in res.stdout


def test_cli_results_list_filters(tmp_project: Path) -> None:
    """'algodesk results list' filters by --type and --study."""
    results_dir = tmp_project / "results"
    _create_mock_results_folder(results_dir, "run_bt_study1", mode="backtest", study="opt_alpha")
    _create_mock_results_folder(results_dir, "run_paper_live", mode="paper")

    runner = CliRunner()
    # List all
    res_all = runner.invoke(app, ["results", "list"])
    assert res_all.exit_code == 0
    assert "run_bt_study1" in res_all.stdout
    assert "run_paper_live" in res_all.stdout

    # Filter --type backtest
    res_bt = runner.invoke(app, ["results", "list", "--type", "backtest"])
    assert res_bt.exit_code == 0
    assert "run_bt_study1" in res_bt.stdout
    assert "run_paper_live" not in res_bt.stdout

    # Filter --study opt_alpha
    res_st = runner.invoke(app, ["results", "list", "--study", "opt_alpha"])
    assert res_st.exit_code == 0
    assert "run_bt_study1" in res_st.stdout


def test_cli_results_show_displays_summary_and_metrics_table(tmp_project: Path) -> None:
    """'algodesk results show <run>' displays SUMMARY text plus metrics table."""
    results_dir = tmp_project / "results"
    _create_mock_results_folder(results_dir, "run_show_1", net_pnl=2450.5, trades=18)

    runner = CliRunner()
    res = runner.invoke(app, ["results", "show", "run_show_1"])
    assert res.exit_code == 0
    assert "Run Summary: run_show_1" in res.stdout
    assert "METRIC" in res.stdout
    assert "VALUE" in res.stdout
    assert "₹2450.50" in res.stdout


def test_cli_results_delete_dry_run(tmp_project: Path) -> None:
    """'algodesk results delete <run> --reason "..." --dry-run' simulates deletion."""
    results_dir = tmp_project / "results"
    folder = _create_mock_results_folder(results_dir, "run_del_dry")

    runner = CliRunner()
    res = runner.invoke(
        app, ["results", "delete", "run_del_dry", "--reason", "test dry run", "--dry-run"]
    )
    assert res.exit_code == 0
    assert "[dry-run] Would delete results bundle run_del_dry" in res.stdout
    assert "Journal database is never touched." in res.stdout
    # Folder still exists
    assert folder.is_dir()


def test_cli_results_delete_confirmation_prompt(tmp_project: Path) -> None:
    """'algodesk results delete <run>' requires typing 'DELETE <run_id>' when --yes omitted."""
    results_dir = tmp_project / "results"
    _create_mock_results_folder(results_dir, "run_prompt_del")

    runner = CliRunner()
    # Wrong confirmation phrase -> abort
    res_wrong = runner.invoke(
        app,
        ["results", "delete", "run_prompt_del", "--reason", "cleanup"],
        input="wrong phrase\n",
    )
    assert res_wrong.exit_code != 0
    assert "Deletion aborted" in res_wrong.stdout

    # Correct confirmation phrase -> delete
    res_ok = runner.invoke(
        app,
        ["results", "delete", "run_prompt_del", "--reason", "cleanup"],
        input="DELETE run_prompt_del\n",
    )
    assert res_ok.exit_code == 0
    assert "Deleted results bundle run_prompt_del" in res_ok.stdout
    assert not (results_dir / "run_prompt_del").exists()


def test_cli_results_delete_with_yes_flag(tmp_project: Path) -> None:
    """'algodesk results delete <run> --yes' moves folder to trash and updates index."""
    results_dir = tmp_project / "results"
    folder = _create_mock_results_folder(results_dir, "run_del_yes")

    # Index first
    runner = CliRunner()
    runner.invoke(app, ["results", "index", "rebuild"])

    res = runner.invoke(
        app, ["results", "delete", "run_del_yes", "--reason", "obsolete trial", "--yes"]
    )
    assert res.exit_code == 0
    assert "Deleted results bundle run_del_yes" in res.stdout
    assert "Journal database untouched." in res.stdout

    assert not folder.exists()
    trash_items = list((results_dir / ".trash").glob("*/run_del_yes"))
    assert len(trash_items) == 1


def test_compare_backtest_runs_equal(tmp_path: Path) -> None:
    """Backtest compare reports hashes equal: True and metrics table when identical."""
    results_dir = tmp_path / "results"
    dir_a = _create_mock_results_folder(results_dir, "bt_cmp_a", result_hash="same_hash_123")
    dir_b = _create_mock_results_folder(results_dir, "bt_cmp_b", result_hash="same_hash_123")

    h_equal, diff = compare_backtest_manifests(dir_a, dir_b)
    assert h_equal is True
    assert diff is None

    tbl = format_backtest_compare_table(dir_a, dir_b)
    assert "Net P&L (₹)" in tbl
    assert "Win Rate (%)" in tbl


def test_compare_backtest_runs_differing_input(tmp_path: Path) -> None:
    """Backtest compare identifies the first differing manifest input when hashes differ."""
    results_dir = tmp_path / "results"
    dir_a = _create_mock_results_folder(results_dir, "bt_cmp_diff_a", result_hash="hash_a")
    dir_b = _create_mock_results_folder(results_dir, "bt_cmp_diff_b", result_hash="hash_b")

    # Modify seed in manifest b
    man_b = json.loads((dir_b / "manifest.json").read_text())
    man_b["seed"] = 999
    (dir_b / "manifest.json").write_text(json.dumps(man_b))

    h_equal, diff = compare_backtest_manifests(dir_a, dir_b)
    assert h_equal is False
    assert diff == "seed: 42 != 999"
