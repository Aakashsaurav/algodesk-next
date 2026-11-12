"""CLI command for inspecting execution session reports.

Displays execution metrics, p95 timings, feed latency, ticks per instrument,
and overall session verdict for operators.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from algodesk.api.errors import ConfigError
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.logs.formatting import format_ist

session_app = typer.Typer(help="Session reporting and operations.")


def _resolve_target_dir(results_dir: Path, run_id: str | None) -> Path:
    if not results_dir.is_dir():
        raise ConfigError(
            what="Results directory does not exist",
            where=str(results_dir),
            fix="Run a paper or replay session before viewing session reports",
        )
    if run_id:
        target_dir = results_dir / run_id
        if not target_dir.is_dir():
            candidates = list(results_dir.glob(f"*{run_id}*"))
            if candidates:
                return candidates[0]
            raise ConfigError(
                what=f"Session run '{run_id}' not found",
                where=str(results_dir),
                fix="Use 'algodesk results list' to view available runs",
            )
        return target_dir

    dirs = [p for p in results_dir.iterdir() if p.is_dir() and (p / "metrics.json").exists()]
    if not dirs:
        raise ConfigError(
            what="No completed session runs found",
            where=str(results_dir),
            fix="Run a paper or replay session before viewing session reports",
        )
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return dirs[0]


def _print_manifest_versions(target_dir: Path) -> None:
    man_file = target_dir / "manifest.json"
    if not man_file.is_file():
        return
    try:
        m_info = json.loads(man_file.read_text(encoding="utf-8"))
        c_ver = m_info.get("code_version")
        if c_ver:
            typer.echo(f"Code Version:         {c_ver}")
        p_info = m_info.get("plugin_versions")
        if isinstance(p_info, dict) and p_info:
            p_str = ", ".join(f"{k}:{v.get('version', '?')}" for k, v in sorted(p_info.items()))
            typer.echo(f"Plugin Versions:      {p_str}")
    except Exception:
        pass


def _print_metrics_details(metrics: dict[str, Any]) -> None:
    cov_pct = float(metrics.get("session_coverage_pct", 0.0))
    win_rate = float(metrics.get("win_rate", 0.0))
    gross_win_rate = float(metrics.get("gross_win_rate", win_rate))
    closed_rt = int(metrics.get("closed_round_trips", 0))
    feed_delay = float(metrics.get("feed_delay_p95", metrics.get("feed_delay_p95_ms", 0.0)))
    plat_lat = float(
        metrics.get(
            "platform_p95",
            metrics.get("platform_p95_ms", metrics.get("p95_latency_ms", 0.0)),
        )
    )
    bar_delay = float(
        metrics.get(
            "bar_close_delay_p95",
            metrics.get(
                "bar_close_delay_p95_ms",
                metrics.get("p95_bar_close_delay_ms", metrics.get("bar_close_delay_ms", 0.0)),
            ),
        )
    )
    rec_rows = int(metrics.get("recorder_rows", 0))
    rec_bytes = int(metrics.get("recorder_bytes", 0))
    rec_vol = float(metrics.get("recorder_mb_per_inst_hour", 0.0))
    proj_vol = float(metrics.get("projected_full_day_mb", 0.0))
    j_growth = int(metrics.get("journal_growth_bytes", 0))

    typer.echo(f"Session Coverage:     {cov_pct:.1f}%")
    typer.echo(f"Win Rate:             {win_rate:.1f}% (Gross: {gross_win_rate:.1f}%)")
    typer.echo(f"Closed Round Trips:   {closed_rt}")
    typer.echo(f"Feed Gaps:            {int(metrics.get('feed_gaps', 0))}")
    typer.echo(f"Signals:              {int(metrics.get('signals', 0))}")
    typer.echo(f"Orders:               {int(metrics.get('orders', 0))}")
    typer.echo(f"Fills:                {int(metrics.get('trades', metrics.get('fills', 0)))}")
    rej = metrics.get("rejects_by_rule", {})
    typer.echo(f"Rejects by Rule:      {rej if rej else 'None'}")
    typer.echo(f"Feed Delay (p95):     {feed_delay:.2f} ms")
    typer.echo(f"Platform (p95):       {plat_lat:.2f} ms")
    typer.echo(f"Bar-Close Delay (p95):{bar_delay:.2f} ms")
    typer.echo(f"Peak RSS:             {float(metrics.get('peak_rss_mb', 0.0)):.1f} MB")
    typer.echo(f"Recorder Written:     {rec_rows} rows ({rec_bytes} bytes)")
    if rec_vol > 0 or proj_vol > 0:
        typer.echo(
            f"Recorder Volume:      {rec_vol:.2f} MB / inst-hr "
            f"(Projected full-day: {proj_vol:.2f} MB)"
        )
    typer.echo(f"Journal Growth:       {j_growth} bytes")


@session_app.command("report")
@handle_cli_errors
def session_report_cmd(
    ctx: typer.Context,
    run_id: str | None = typer.Argument(
        None, help="Run ID of the session (defaults to most recent run)"
    ),
) -> None:
    """Print comprehensive observability report for a past or active execution session."""
    with _open_rt(ctx, ensure=False) as rt:
        target_dir = _resolve_target_dir(rt.paths.results, run_id)
        metrics_file = target_dir / "metrics.json"
        metrics: dict[str, Any] = (
            json.loads(metrics_file.read_text()) if metrics_file.exists() else {}
        )
        session_text = (
            (target_dir / "session.md").read_text() if (target_dir / "session.md").exists() else ""
        )
        summary_text = (
            (target_dir / "SUMMARY.md").read_text() if (target_dir / "SUMMARY.md").exists() else ""
        )
        verdict = (
            "STOPPED"
            if (
                "Verdict: STOPPED" in session_text
                or "Verdict: STOPPED" in summary_text
                or metrics.get("stop_reason") == "interrupt"
            )
            else (
                "FAIL"
                if ("Verdict: FAIL" in session_text or "Verdict: FAIL" in summary_text)
                else "PASS"
            )
        )
        is_partial = (
            metrics.get("is_partial", False)
            or "(partial)" in summary_text
            or "(partial)" in session_text
        )
        if is_partial:
            verdict = f"{verdict} (partial)"

        typer.echo("=" * 60)
        typer.echo(f"Session Report: {target_dir.name}")
        typer.echo("=" * 60)
        typer.echo(f"Verdict:              {verdict}")
        open_pos = metrics.get("open_positions", [])
        if not open_pos and (target_dir / "trades.csv").is_file():
            try:
                import csv

                with open(target_dir / "trades.csv", newline="", encoding="utf-8") as tf:
                    t_rows = list(csv.DictReader(tf))
                net_pos: dict[str, int] = {}
                for tr in t_rows:
                    i_key = tr.get("instrument", "")
                    if not i_key:
                        continue
                    q = int(tr.get("qty", 0))
                    s = str(tr.get("side", "")).upper()
                    net_pos[i_key] = net_pos.get(i_key, 0) + (q if s == "BUY" else -q)
                open_pos = [
                    {"instrument": k, "product": "MIS", "qty": v}
                    for k, v in net_pos.items()
                    if v != 0
                ]
            except Exception:
                pass

        verdict_note = None
        for line in session_text.splitlines():
            if line.startswith("Verdict Note:"):
                verdict_note = line.replace("Verdict Note:", "").strip()
        if not verdict_note and open_pos:
            verdict_note = f"Unresolved open positions at stop ({len(open_pos)})"
        if verdict_note:
            typer.echo(f"Verdict Note:         {verdict_note}")

        stop_reason = metrics.get("stop_reason")
        if not stop_reason:
            for line in session_text.splitlines():
                if line.startswith("Stop Reason:"):
                    stop_reason = line.replace("Stop Reason:", "").strip()
        if stop_reason:
            typer.echo(f"Stop Reason:          {stop_reason}")
        fail_reason = metrics.get("fail_reason")
        if verdict.startswith("FAIL") and not fail_reason:
            for line in session_text.splitlines():
                if line.startswith("Error:"):
                    fail_reason = line.replace("Error:", "").strip()
        if verdict.startswith("FAIL") and fail_reason:
            typer.echo(f"Fail Reason:          {fail_reason}")
        _print_manifest_versions(target_dir)

        mode_val = str(metrics.get("mode", "")).lower()
        if mode_val == "replay" or "_replay_" in target_dir.name:
            typer.echo("Run Type:             replay")
        elif is_partial:
            typer.echo("Run Type:             partial (started after session open)")

        first_tick = format_ist(metrics.get("first_tick_ist") or metrics.get("first_tick_ts"))
        last_tick = format_ist(metrics.get("last_tick_ist") or metrics.get("last_tick_ts"))
        typer.echo(f"First Tick (IST):     {first_tick}")
        typer.echo(f"Last Tick (IST):      {last_tick}")
        _print_metrics_details(metrics)

        ticks_per_inst = metrics.get("ticks_per_instrument", {})
        if ticks_per_inst:
            typer.echo("\nTicks per Instrument:")
            tick_rows = [[k, str(v)] for k, v in ticks_per_inst.items()]
            typer.echo(format_table(["INSTRUMENT", "TICKS"], tick_rows))
        else:
            typer.echo("Ticks per Instrument: None")

        bars_by_tf = metrics.get("bars_by_timeframe", {})
        if bars_by_tf:
            typer.echo("\nBars per Timeframe:")
            b_rows = [[k, str(v)] for k, v in bars_by_tf.items()]
            typer.echo(format_table(["TIMEFRAME", "BARS"], b_rows))
        else:
            typer.echo("Bars per Timeframe:   None")

        if open_pos:
            typer.echo("\nOpen at stop:")
            pos_table = [
                [p["instrument"], str(p.get("product", "MIS")), str(p["qty"])] for p in open_pos
            ]
            typer.echo(format_table(["INSTRUMENT", "PRODUCT", "QTY"], pos_table))
        else:
            typer.echo("\nOpen at stop:         None")
