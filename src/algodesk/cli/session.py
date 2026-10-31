"""CLI command for inspecting execution session reports.

M1 Design 8.1 & 8.2: Session observability and results reporting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from algodesk.api.errors import ConfigError
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors

session_app = typer.Typer(help="Session reporting and operations.")


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
        results_dir = rt.paths.results
        if not results_dir.is_dir():
            raise ConfigError(
                what="Results directory does not exist",
                where=str(results_dir),
                fix="Run a paper or replay session before viewing session reports",
            )

        target_dir: Path | None = None
        if run_id:
            target_dir = results_dir / run_id
            if not target_dir.is_dir():
                candidates = list(results_dir.glob(f"*{run_id}*"))
                if candidates:
                    target_dir = candidates[0]
                else:
                    raise ConfigError(
                        what=f"Session run '{run_id}' not found",
                        where=str(results_dir),
                        fix="Use 'algodesk results list' to view available runs",
                    )
        else:
            dirs = [
                p for p in results_dir.iterdir() if p.is_dir() and (p / "metrics.json").exists()
            ]
            if not dirs:
                raise ConfigError(
                    what="No completed session runs found",
                    where=str(results_dir),
                    fix="Run a paper or replay session before viewing session reports",
                )
            dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            target_dir = dirs[0]

        metrics_file = target_dir / "metrics.json"
        metrics: dict[str, Any] = {}
        if metrics_file.exists():
            try:
                metrics = json.loads(metrics_file.read_text())
            except Exception:
                pass

        session_md_file = target_dir / "session.md"
        session_text = session_md_file.read_text() if session_md_file.exists() else ""

        summary_md_file = target_dir / "SUMMARY.md"
        summary_text = summary_md_file.read_text() if summary_md_file.exists() else ""

        verdict = "PASS"
        if "Verdict: FAIL" in session_text or "Verdict: FAIL" in summary_text:
            verdict = "FAIL"
        is_partial = (
            metrics.get("is_partial", False)
            or "(partial)" in summary_text
            or "(partial)" in session_text
        )
        if is_partial and "(partial)" not in verdict:
            verdict = f"{verdict} (partial)"

        first_tick = metrics.get("first_tick_ist", "N/A")
        last_tick = metrics.get("last_tick_ist", "N/A")
        cov_pct = float(metrics.get("session_coverage_pct", 0.0))
        ticks_per_inst: dict[str, Any] = metrics.get("ticks_per_instrument", {})
        feed_gaps = int(metrics.get("feed_gaps", 0))
        bars_by_tf: dict[str, Any] = metrics.get("bars_by_timeframe", {})
        signals = int(metrics.get("signals", 0))
        orders = int(metrics.get("orders", 0))
        fills = int(metrics.get("trades", metrics.get("fills", 0)))
        rejects = metrics.get("rejects_by_rule", {})
        p95_lat = float(metrics.get("p95_latency_ms", 0.0))
        peak_rss = float(metrics.get("peak_rss_mb", 0.0))
        rec_rows = int(metrics.get("recorder_rows", 0))
        rec_bytes = int(metrics.get("recorder_bytes", 0))
        j_growth = int(metrics.get("journal_growth_bytes", 0))
        fail_reason = metrics.get("fail_reason")

        if verdict.startswith("FAIL") and not fail_reason:
            for line in session_text.splitlines():
                if line.startswith("Error:"):
                    fail_reason = line.replace("Error:", "").strip()

        typer.echo("=" * 60)
        typer.echo(f"Session Report: {target_dir.name}")
        typer.echo("=" * 60)
        typer.echo(f"Verdict:              {verdict}")
        if verdict.startswith("FAIL") and fail_reason:
            typer.echo(f"Fail Reason:          {fail_reason}")
        if is_partial:
            typer.echo("Run Type:             partial (started after session open)")
        typer.echo(f"First Tick (IST):     {first_tick}")
        typer.echo(f"Last Tick (IST):      {last_tick}")
        typer.echo(f"Session Coverage:     {cov_pct:.1f}%")
        typer.echo(f"Feed Gaps:            {feed_gaps}")
        typer.echo(f"Signals:              {signals}")
        typer.echo(f"Orders:               {orders}")
        typer.echo(f"Fills:                {fills}")
        typer.echo(f"Rejects by Rule:      {rejects if rejects else 'None'}")
        typer.echo(f"Latency (p95):        {p95_lat:.2f} ms")
        typer.echo(f"Peak RSS:             {peak_rss:.1f} MB")
        typer.echo(f"Recorder Written:     {rec_rows} rows ({rec_bytes} bytes)")
        typer.echo(f"Journal Growth:       {j_growth} bytes")

        if ticks_per_inst:
            typer.echo("\nTicks per Instrument:")
            t_rows = [[k, str(v)] for k, v in ticks_per_inst.items()]
            typer.echo(format_table(["INSTRUMENT", "TICKS"], t_rows))
        else:
            typer.echo("Ticks per Instrument: None")

        if bars_by_tf:
            typer.echo("\nBars per Timeframe:")
            b_rows = [[k, str(v)] for k, v in bars_by_tf.items()]
            typer.echo(format_table(["TIMEFRAME", "BARS"], b_rows))
        else:
            typer.echo("Bars per Timeframe:   None")
