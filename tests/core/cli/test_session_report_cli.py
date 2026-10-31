"""Tests for 'algodesk session report' CLI command and observability report outputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from algodesk.cli.app import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_session_report_without_results_dir_raises_error(tmp_project: Path, runner: CliRunner) -> None:
    results_dir = tmp_project / "results"
    if results_dir.exists():
        import shutil
        shutil.rmtree(results_dir)
    res = runner.invoke(app, ["--root", str(tmp_project), "session", "report"])
    assert res.exit_code == 1
    assert "Results directory does not exist" in res.output


def test_session_report_with_no_completed_runs(tmp_project: Path, runner: CliRunner) -> None:
    results_dir = tmp_project / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    for p in results_dir.iterdir():
        if p.is_dir():
            for f in p.iterdir():
                f.unlink(missing_ok=True)
            p.rmdir()

    res = runner.invoke(app, ["--root", str(tmp_project), "session", "report"])
    assert res.exit_code == 1
    assert "No completed session runs found" in res.output


def test_session_report_nonexistent_run_id(tmp_project: Path, runner: CliRunner) -> None:
    results_dir = tmp_project / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    res = runner.invoke(app, ["--root", str(tmp_project), "session", "report", "nonexistent_run_id_xyz"])
    assert res.exit_code == 1
    assert "not found" in res.output


def test_session_report_prints_all_14_required_fields(tmp_project: Path, runner: CliRunner) -> None:
    # Generate replay run
    res_replay = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "replay",
            "--dates",
            "2026-10-26",
            "--run",
            "demo_paper",
            "--quiet",
        ],
    )
    assert res_replay.exit_code == 0

    res_report = runner.invoke(app, ["--root", str(tmp_project), "session", "report"])
    assert res_report.exit_code == 0

    out = res_report.output
    # 1. Verdict
    assert "Verdict:" in out
    # 2. First tick (IST)
    assert "First Tick (IST):" in out
    # 3. Last tick (IST)
    assert "Last Tick (IST):" in out
    # 4. Session coverage %
    assert "Session Coverage:" in out
    # 5. Feed gaps
    assert "Feed Gaps:" in out
    # 6. Signals
    assert "Signals:" in out
    # 7. Orders
    assert "Orders:" in out
    # 8. Fills
    assert "Fills:" in out
    # 9. Rejects by rule
    assert "Rejects by Rule:" in out
    # 10. Latency (p95)
    assert "Latency (p95):" in out
    # 11. Peak RSS
    assert "Peak RSS:" in out
    # 12. Recorder rows & bytes
    assert "Recorder Written:" in out
    # 13. Journal growth
    assert "Journal Growth:" in out
    # 14. Ticks per instrument
    assert "Ticks per Instrument:" in out


def test_session_report_with_explicit_run_id(tmp_project: Path, runner: CliRunner) -> None:
    results_dir = tmp_project / "results"
    run_dir = results_dir / "custom_test_session_001"
    run_dir.mkdir(parents=True, exist_ok=True)

    metrics = {
        "date": "2026-10-30",
        "mode": "paper",
        "first_tick_ist": "2026-10-30 09:16:00 IST",
        "last_tick_ist": "2026-10-30 15:28:00 IST",
        "session_coverage_pct": 99.2,
        "feed_gaps": 2,
        "signals": 15,
        "orders": 8,
        "fills": 8,
        "rejects_by_rule": {"max_notional": 1},
        "p95_latency_ms": 4.5,
        "peak_rss_mb": 145.2,
        "recorder_rows": 12500,
        "recorder_bytes": 450000,
        "journal_growth_bytes": 18200,
        "is_partial": True,
        "ticks_per_instrument": {"NSE:EQ:RELIANCE": 5000, "NSE:EQ:TCS": 7500},
        "bars_by_timeframe": {"1m": 375, "5m": 75},
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (run_dir / "SUMMARY.md").write_text(
        "# Run Summary\nVerdict: PASS (partial)\n", encoding="utf-8"
    )

    res = runner.invoke(app, ["--root", str(tmp_project), "session", "report", "custom_test_session_001"])
    assert res.exit_code == 0
    out = res.output
    assert "custom_test_session_001" in out
    assert "PASS (partial)" in out
    assert "partial (started after session open)" in out
    assert "2026-10-30 09:16:00 IST" in out
    assert "2026-10-30 15:28:00 IST" in out
    assert "99.2%" in out
    assert "Feed Gaps:            2" in out
    assert "Signals:              15" in out
    assert "Orders:               8" in out
    assert "Fills:                8" in out
    assert "max_notional" in out
    assert "4.50 ms" in out
    assert "145.2 MB" in out
    assert "12500 rows (450000 bytes)" in out
    assert "18200 bytes" in out
    assert "NSE:EQ:RELIANCE" in out
    assert "5000" in out
    assert "NSE:EQ:TCS" in out
    assert "7500" in out
    assert "1m" in out
    assert "375" in out


def test_session_report_with_fail_verdict_and_reason(tmp_project: Path, runner: CliRunner) -> None:
    results_dir = tmp_project / "results"
    run_dir = results_dir / "fail_session_002"
    run_dir.mkdir(parents=True, exist_ok=True)

    metrics = {
        "date": "2026-10-30",
        "mode": "paper",
        "fail_reason": "WebSocket connection closed by broker with code 401",
        "is_partial": False,
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (run_dir / "SUMMARY.md").write_text(
        "# Run Summary\nVerdict: FAIL\nReason: WebSocket connection closed by broker with code 401\n",
        encoding="utf-8",
    )
    (run_dir / "session.md").write_text(
        "Verdict: FAIL\nError: WebSocket connection closed by broker with code 401\n",
        encoding="utf-8",
    )

    res = runner.invoke(app, ["--root", str(tmp_project), "session", "report", "fail_session_002"])
    assert res.exit_code == 0
    out = res.output
    assert "Verdict:              FAIL" in out
    assert "Fail Reason:          WebSocket connection closed by broker with code 401" in out


def test_demo_paper_fast_run_config_exists_and_dry_run_works(runner: CliRunner) -> None:
    res = runner.invoke(app, ["run", "demo_paper_fast", "--dry-run"])
    assert res.exit_code == 0
    assert "Dry-Run Execution Plan" in res.output
    assert "demo_paper_fast" in res.output
    assert "Doctor checks:     0 critical, 0 warnings" in res.output
    assert "Plan verified successfully" in res.output


def test_demo_paper_fast_prepare_works(runner: CliRunner) -> None:
    res = runner.invoke(app, ["run", "demo_paper_fast", "--prepare"])
    assert res.exit_code == 0
    assert "demo_paper_fast" in res.output
    assert "All required plugins passed conformance" in res.output
    assert "Doctor pre-start check: 0 critical" in res.output
