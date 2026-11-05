"""Tests for results compare functionality and replay determinism acceptance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import ConfigError
from algodesk.cli.app import app
from algodesk.data.manifest import DataManifest
from algodesk.data.synth import synth_ticks
from algodesk.results.compare import compare_runs

runner = CliRunner()


def test_compare_runs_missing_bundle(tmp_project: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        compare_runs(tmp_project, "non_existent_a", "non_existent_b")


def test_compare_runs_identical_and_different(tmp_project: Path) -> None:
    results_dir = tmp_project / "results"
    run_a = results_dir / "run_alpha"
    run_b = results_dir / "run_beta"
    run_a.mkdir(parents=True, exist_ok=True)
    run_b.mkdir(parents=True, exist_ok=True)

    metrics_a = {
        "trades": 2,
        "gross_pnl": 500.0,
        "net_pnl": 450.0,
        "charges": 50.0,
        "win_rate": 100.0,
        "max_drawdown": 1.2,
    }
    (run_a / "metrics.json").write_text(json.dumps(metrics_a))
    (run_b / "metrics.json").write_text(json.dumps(metrics_a))

    p_sig = {"strategy": "sma", "instrument": "NSE:EQ:RELIANCE", "side": "BUY", "qty": 10}
    p_dec = {"allow": True, "rule": "limits", "reason": "ok"}
    p_ord = {
        "spec": {
            "instrument": "NSE:EQ:RELIANCE",
            "side": "BUY",
            "qty": 10,
            "type": "MARKET",
            "price": "100.0",
            "product": "CNC",
        }
    }
    p_fil = {
        "instrument": "NSE:EQ:RELIANCE",
        "side": "BUY",
        "qty": 10,
        "price": "100.0",
        "charges_total": "5.0",
    }
    evs = [
        {"kind": "signal", "payload_json": json.dumps(p_sig)},
        {"kind": "risk.decision", "payload_json": json.dumps(p_dec)},
        {"kind": "order.created", "payload_json": json.dumps(p_ord)},
        {"kind": "fill", "payload_json": json.dumps(p_fil)},
    ]
    with open(run_a / "events.jsonl", "w") as f:
        for ev in evs:
            f.write(json.dumps(ev) + "\n")
    with open(run_b / "events.jsonl", "w") as f:
        for ev in evs:
            f.write(json.dumps(ev) + "\n")

    # Identical comparison
    eq, diff = compare_runs(tmp_project, "run_alpha", "run_beta")
    assert eq is True
    assert diff is None

    # Different metrics
    metrics_diff = dict(metrics_a, trades=3)
    (run_b / "metrics.json").write_text(json.dumps(metrics_diff))
    eq, diff = compare_runs(tmp_project, "run_alpha", "run_beta")
    assert eq is False
    assert "metrics.trades" in str(diff)

    # Revert metrics, modify fill in run_b
    (run_b / "metrics.json").write_text(json.dumps(metrics_a))
    evs_diff = list(evs)
    p_fil_diff = {
        "instrument": "NSE:EQ:RELIANCE",
        "side": "BUY",
        "qty": 5,
        "price": "100.0",
        "charges_total": "5.0",
    }
    evs_diff[-1] = {"kind": "fill", "payload_json": json.dumps(p_fil_diff)}
    with open(run_b / "events.jsonl", "w") as f:
        for ev in evs_diff:
            f.write(json.dumps(ev) + "\n")

    eq, diff = compare_runs(tmp_project, "run_alpha", "run_beta")
    assert eq is False
    assert "fills[0]" in str(diff)


def test_cli_results_compare(tmp_project: Path) -> None:
    results_dir = tmp_project / "results"
    run_a = results_dir / "cli_run_a"
    run_b = results_dir / "cli_run_b"
    run_a.mkdir(parents=True, exist_ok=True)
    run_b.mkdir(parents=True, exist_ok=True)

    metrics = {"trades": 1, "net_pnl": 100.0}
    (run_a / "metrics.json").write_text(json.dumps(metrics))
    (run_b / "metrics.json").write_text(json.dumps(metrics))

    res = runner.invoke(
        app, ["--root", str(tmp_project), "results", "compare", "cli_run_a", "cli_run_b"]
    )
    assert res.exit_code == 0
    assert "equal: True" in res.stdout

    res_json = runner.invoke(
        app,
        ["--root", str(tmp_project), "--json", "results", "compare", "cli_run_a", "cli_run_b"],
    )
    assert res_json.exit_code == 0
    data = json.loads(res_json.stdout)
    assert data["equal"] is True


def test_replay_acceptance_trend_twice(tmp_project: Path) -> None:
    """Step 9: Synth trend data, run replay --twice, assert trades >= 2 and equal sequences."""
    data_dir = tmp_project / "data"
    manifest_db = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(manifest_db)

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

    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "replay",
            "--dates",
            "2026-10-26",
            "--run",
            "demo_paper",
            "--twice",
        ],
    )
    assert res.exit_code == 0, res.stdout
    assert "replay equal: True" in res.stdout

    # Check results list shows mode replay and trades >= 2
    res_list = runner.invoke(app, ["--root", str(tmp_project), "results", "list"])
    assert res_list.exit_code == 0
    assert "replay" in res_list.stdout
    # Verify at least one run has trades >= 2
    found_trades = False
    for line in res_list.stdout.splitlines():
        if "replay" in line:
            parts = line.split()
            # columns: RUN_ID, DATE, MODE, TRADES, NET_PNL, VERDICT
            if len(parts) >= 4 and parts[3].isdigit() and int(parts[3]) >= 2:
                found_trades = True
                break
    assert found_trades, f"Expected replay run with trades >= 2, got:\n{res_list.stdout}"


def test_compare_plugin_version_diffs_only_for_configured_plugins(tmp_project: Path) -> None:
    """results compare reports version differences only for plugins in run config."""
    from algodesk.results.compare import compare_runs

    results_dir = tmp_project / "results"
    run_a = results_dir / "demo_paper_paper_test_a"
    run_b = results_dir / "demo_paper_replay_test_b"
    run_a.mkdir(parents=True, exist_ok=True)
    run_b.mkdir(parents=True, exist_ok=True)

    metrics = {
        "trades": 0,
        "gross_pnl": 0.0,
        "net_pnl": 0.0,
        "charges": 0.0,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
    }
    (run_a / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (run_b / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")

    # Both runs configured with 'upstox', but NOT 'nse_bhavcopy'
    cfg_text = "data_source.plugin = 'upstox'\n"
    (run_a / "config.effective.txt").write_text(cfg_text, encoding="utf-8")
    (run_b / "config.effective.txt").write_text(cfg_text, encoding="utf-8")

    # In manifest: nse_bhavcopy has different versions, upstox has identical versions
    man_a = {
        "code_version": "0.1.0",
        "plugin_versions": {
            "nse_bhavcopy": {"version": "0.1.0", "hash": "h1"},
            "upstox": {"version": "0.1.0", "hash": "u1"},
        },
    }
    man_b = {
        "code_version": "0.1.0",
        "plugin_versions": {
            "nse_bhavcopy": {"version": "0.2.0", "hash": "h2"},
            "upstox": {"version": "0.1.0", "hash": "u1"},
        },
    }
    (run_a / "manifest.json").write_text(json.dumps(man_a), encoding="utf-8")
    (run_b / "manifest.json").write_text(json.dumps(man_b), encoding="utf-8")

    is_equal, diff = compare_runs(tmp_project, run_a.name, run_b.name)
    # Since nse_bhavcopy is not configured, its version difference should NOT cause divergence!
    assert is_equal is True
    assert diff is None
