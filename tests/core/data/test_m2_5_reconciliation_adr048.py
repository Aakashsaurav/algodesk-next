"""Tests for corporate action reconciler per ADR-048, compare runs, and session report."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from algodesk.api.market import ActionRecord
from algodesk.cli.app import app
from algodesk.data.reconciler import CorporateActionsReconciler
from algodesk.results.compare import compare_runs


def test_reconciler_two_agree_on_split_ratio() -> None:
    a1 = ActionRecord(
        ex_date=date(2021, 10, 28),
        type="split",
        ratio=5.0,
        source="nse",
        symbol="IRCTC",
    )
    a2 = ActionRecord(
        ex_date=date(2021, 10, 28),
        type="split",
        ratio=5.0,
        source="yfinance",
        symbol="IRCTC",
    )
    rec = CorporateActionsReconciler().reconcile("NSE", "IRCTC", [a1, a2])
    assert len(rec) == 1
    assert rec[0].status == "confirmed"
    assert rec[0].ratio == 5.0


def test_reconciler_two_agree_on_bonus_ratio() -> None:
    a1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        source="nse",
        symbol="RELIANCE",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        source="yfinance",
        symbol="RELIANCE",
    )
    rec = CorporateActionsReconciler().reconcile("NSE", "RELIANCE", [a1, a2])
    assert len(rec) == 1
    assert rec[0].status == "confirmed"


def test_reconciler_split_ratio_conflict() -> None:
    a1 = ActionRecord(
        ex_date=date(2021, 10, 28),
        type="split",
        ratio=5.0,
        source="nse",
        symbol="IRCTC",
    )
    a2 = ActionRecord(
        ex_date=date(2021, 10, 28),
        type="split",
        ratio=2.0,
        source="yfinance",
        symbol="IRCTC",
    )
    rec = CorporateActionsReconciler().reconcile("NSE", "IRCTC", [a1, a2])
    assert len(rec) == 1
    assert rec[0].status == "conflict"
    assert "ratio disagreement" in rec[0].reason


def test_reconciler_bonus_ratio_conflict() -> None:
    a1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        source="nse",
        symbol="RELIANCE",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="yfinance",
        symbol="RELIANCE",
    )
    rec = CorporateActionsReconciler().reconcile("NSE", "RELIANCE", [a1, a2])
    assert len(rec) == 1
    assert rec[0].status == "conflict"


def test_reconciler_dividend_amount_conflict() -> None:
    a1 = ActionRecord(
        ex_date=date(2024, 8, 19),
        type="dividend",
        amount=Decimal("10.00"),
        source="nse",
        symbol="RELIANCE",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 8, 19),
        type="dividend",
        amount=Decimal("12.00"),
        source="yfinance",
        symbol="RELIANCE",
    )
    rec = CorporateActionsReconciler().reconcile("NSE", "RELIANCE", [a1, a2])
    assert len(rec) == 1
    assert rec[0].status == "conflict"


def test_reconciler_split_seen_by_one_source_only_stays_unverified() -> None:
    a1 = ActionRecord(
        ex_date=date(2021, 10, 28),
        type="split",
        ratio=5.0,
        source="nse",
        symbol="IRCTC",
    )
    rec = CorporateActionsReconciler().reconcile("NSE", "IRCTC", [a1])
    assert len(rec) == 1
    assert rec[0].status == "unverified"


def test_reconciler_bonus_seen_by_one_source_only_stays_unverified() -> None:
    a1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        source="yfinance",
        symbol="RELIANCE",
    )
    rec = CorporateActionsReconciler().reconcile("NSE", "RELIANCE", [a1])
    assert len(rec) == 1
    assert rec[0].status == "unverified"


def test_reconciler_date_disagreement_across_sources_conflict() -> None:
    a1 = ActionRecord(
        ex_date=date(2024, 10, 27),
        type="bonus",
        ratio=1.0,
        source="nse",
        symbol="RELIANCE",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        source="yfinance",
        symbol="RELIANCE",
    )
    rec = CorporateActionsReconciler().reconcile("NSE", "RELIANCE", [a1, a2])
    assert all(r.status == "conflict" for r in rec)
    assert any("ex_date disagreement" in r.reason for r in rec)


def test_compare_runs_shows_code_version_difference(tmp_path: Path) -> None:
    res_dir = tmp_path / "results"
    run_a = res_dir / "run_001"
    run_b = res_dir / "run_002"
    run_a.mkdir(parents=True)
    run_b.mkdir(parents=True)

    metrics = {"trades": 5, "net_pnl": 100.0}
    (run_a / "metrics.json").write_text(json.dumps(metrics))
    (run_b / "metrics.json").write_text(json.dumps(metrics))

    man_a = {"code_version": "0.1.0", "plugin_versions": {}}
    man_b = {"code_version": "0.2.0", "plugin_versions": {}}
    (run_a / "manifest.json").write_text(json.dumps(man_a))
    (run_b / "manifest.json").write_text(json.dumps(man_b))

    equal, diff = compare_runs(tmp_path, "run_001", "run_002")
    assert not equal
    assert diff is not None
    assert "code_version: 0.1.0 != 0.2.0" in diff


def test_compare_runs_shows_plugin_version_difference(tmp_path: Path) -> None:
    res_dir = tmp_path / "results"
    run_a = res_dir / "run_001"
    run_b = res_dir / "run_002"
    run_a.mkdir(parents=True)
    run_b.mkdir(parents=True)

    metrics = {"trades": 5, "net_pnl": 100.0}
    (run_a / "metrics.json").write_text(json.dumps(metrics))
    (run_b / "metrics.json").write_text(json.dumps(metrics))

    man_a = {
        "code_version": "0.1.0",
        "plugin_versions": {"upstox": {"version": "0.1.0", "hash": "h1"}},
    }
    man_b = {
        "code_version": "0.1.0",
        "plugin_versions": {"upstox": {"version": "0.2.0", "hash": "h2"}},
    }
    (run_a / "manifest.json").write_text(json.dumps(man_a))
    (run_b / "manifest.json").write_text(json.dumps(man_b))

    equal, diff = compare_runs(tmp_path, "run_001", "run_002")
    assert not equal
    assert diff is not None
    assert "plugin 'upstox' version: 0.1.0 != 0.2.0" in diff


def test_session_report_prints_code_and_plugin_versions(tmp_project: Path) -> None:
    res_dir = tmp_project / "results" / "test_session_versions"
    res_dir.mkdir(parents=True, exist_ok=True)
    metrics = {"date": "2026-10-30", "mode": "paper", "trades": 0}
    (res_dir / "metrics.json").write_text(json.dumps(metrics))
    man = {
        "code_version": "0.1.0",
        "plugin_versions": {"nse_actions": {"version": "0.1.0", "hash": "abc"}},
    }
    (res_dir / "manifest.json").write_text(json.dumps(man))

    runner = CliRunner()
    res = runner.invoke(
        app, ["--root", str(tmp_project), "session", "report", "test_session_versions"]
    )
    assert res.exit_code == 0
    assert "Code Version:         0.1.0" in res.output
    assert "Plugin Versions:      nse_actions:0.1.0" in res.output
