"""AlgoDesk CLI commands for backtesting."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import typer

from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.config.snapshot import build_effective
from algodesk.engine.backtest import BacktestRunner
from algodesk.fees.engine import FeeEngine
from algodesk.ops.fees import check_fees_readiness
from algodesk.ops.readiness import evaluate_backtest_readiness

backtest_app = typer.Typer(help="Execute and manage backtest sessions.")


def _resolve_missing_allowance(eff_run: Any, rt: Any) -> float:
    """Read maximum allowable missing percentage from run config or parameter registry."""
    if eff_run.backtest and eff_run.backtest.max_missing_pct is not None:
        return float(eff_run.backtest.max_missing_pct)
    if rt.params is not None:
        try:
            val = rt.params.get("backtest.max_missing_pct")
            if val is not None:
                return float(val)
        except Exception:
            pass
    return 10.0


def _print_readiness_report(report: Any, allowed_missing: float) -> None:
    """Print tabular formatted readiness report and remediation commands."""
    typer.echo(
        f"=== Backtest Readiness Check: {report.start.isoformat()} to {report.end.isoformat()} ==="
    )
    headers = ["Symbol", "Status", "First Stored", "Last Stored", "Missing Days", "Primary Issue"]
    rows = []
    for sym in report.members:
        sr = report.symbol_reports.get(sym)
        if sr is None or sr.is_ready:
            st = "OK"
            f_d = sr.first_stored_day.isoformat() if sr and sr.first_stored_day else "N/A"
            l_d = sr.last_stored_day.isoformat() if sr and sr.last_stored_day else "N/A"
            m_cnt = "0"
            prob = "None"
        else:
            st = "SKIPPED"
            f_d = sr.first_stored_day.isoformat() if sr.first_stored_day else "none"
            l_d = sr.last_stored_day.isoformat() if sr.last_stored_day else "none"
            m_cnt = str(len(sr.missing_sessions))
            prob = sr.primary_skip_reason or (sr.problems[0] if sr.problems else "data_problem")
        rows.append([sym, st, f_d, l_d, m_cnt, prob])

    typer.echo(format_table(headers, rows))
    typer.echo(
        f"\nUniverse: {len(report.members)} symbols, With Data: {len(report.with_data)} "
        f"({report.coverage_pct:.1f}%), Missing: {report.missing_pct:.1f}% "
        f"(max allowed: {allowed_missing:.1f}%)"
    )

    if report.skipped:
        typer.echo(f"\nSkipped symbols ({len(report.skipped)}):")
        for sk in report.skipped:
            typer.echo(f"  - {sk['symbol']}: {sk['reason']}")

    if report.warnings:
        typer.echo(f"\nWarnings ({len(report.warnings)}):")
        for w in report.warnings:
            typer.echo(f"  - {w['symbol']}: {w['warning']}")
            if w.get("fix"):
                typer.echo(f"    Cleanup: {w['fix']}")

    if report.remediation_commands:
        typer.echo("\nRemediation commands:")
        for cmd in report.remediation_commands:
            typer.echo(f"  {cmd}")


def _print_fees_readiness_report(fee_report: Any) -> None:
    """Print tabular formatted fee readiness check conforming to ADR-084."""
    typer.echo(f"\n=== Fees Readiness: schedule '{fee_report.schedule}' ===")
    headers = ["Component", "Segment", "Product", "Earliest Verified", "Status"]
    rows = []
    for c in fee_report.components:
        rows.append(
            [
                c.name,
                c.segment,
                c.product,
                c.earliest_verified.isoformat(),
                c.status,
            ]
        )
    typer.echo(format_table(headers, rows))
    if fee_report.has_errors:
        typer.echo(f"\nFee check error: {fee_report.error_message}")
        typer.echo(f"Fix: {fee_report.remediation_fix}")


@backtest_app.command("check")
@handle_cli_errors
def backtest_check_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Run configuration name"),
    json_output: bool = typer.Option(False, "--json", help="Output readiness report in JSON"),
    start: str | None = typer.Option(
        None, "--start", help="Override backtest start date YYYY-MM-DD"
    ),
    end: str | None = typer.Option(None, "--end", help="Override backtest end date YYYY-MM-DD"),
) -> None:
    """Check data readiness and universe coverage before backtest execution."""
    with _open_rt(ctx, ensure=True) as rt:
        eff = build_effective(rt.root, run_name=name)
        if eff.run is None:
            raise ConfigError(
                what=f"Run configuration '{name}' not found",
                where=f"config/runs/{name}.toml",
                fix=f"Verify configuration file exists for '{name}'",
            )

        if eff.run.run.mode != Mode.BACKTEST:
            raise ConfigError(
                what=(
                    f"Run configuration '{name}' is in mode '{eff.run.run.mode}', "
                    "expected 'backtest'"
                ),
                where=f"config/runs/{name}.toml",
                fix="Set run.mode = 'backtest' in the run configuration",
            )

        start_d = date.fromisoformat(start.strip()) if start else None
        end_d = date.fromisoformat(end.strip()) if end else None

        report, _ = evaluate_backtest_readiness(
            eff.run,
            rt,
            start_override=start_d,
            end_override=end_d,
        )

        fee_report = check_fees_readiness(
            eff.run,
            rt,
            start_override=start_d,
        )

        allowed_missing = _resolve_missing_allowance(eff.run, rt)

        if json_output:
            combined = report.to_dict()
            combined["fees"] = fee_report.to_dict()
            typer.echo(json.dumps(combined, indent=2))
        else:
            _print_readiness_report(report, allowed_missing)
            _print_fees_readiness_report(fee_report)

        if report.missing_pct > allowed_missing:
            if json_output:
                raise typer.Exit(code=1)
            fix_msg = (
                report.remediation_commands[0]
                if report.remediation_commands
                else "Accumulate missing data"
            )
            raise ConfigError(
                what=(
                    f"Readiness check failed: missing share {report.missing_pct:.1f}% "
                    f"exceeds backtest.max_missing_pct ({allowed_missing:.1f}%)"
                ),
                where=f"backtest.check:{name}",
                fix=fix_msg,
            )

        if fee_report.has_errors:
            if json_output:
                raise typer.Exit(code=1)
            raise ConfigError(
                what=fee_report.error_message,
                where=f"backtest.check:{name}",
                fix=fee_report.remediation_fix,
            )


def _check_sim_fee_hashes(rt: Any, sess_dir: Path, run_config: Any) -> None:
    """Print note if sim or fee plugin code hash differs from recorded paper run."""
    manifest_path = sess_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        man = json.loads(manifest_path.read_text(encoding="utf-8"))
        pv_sess = man.get("plugin_versions") or {}
        curr_plugins = {p.name: p for p in rt.plugins.list()}
        sim_name = getattr(getattr(run_config, "execution_venue", None), "plugin", "sim")
        fee_name = getattr(getattr(run_config, "fees", None), "schedule", "zerodha_nse")
        for p_chk in (sim_name, fee_name, "sim", "zerodha_nse"):
            rec_info = pv_sess.get(p_chk)
            curr_p = curr_plugins.get(p_chk)
            if rec_info and curr_p:
                rec_hash = rec_info.get("hash")
                if rec_hash and curr_p.code_hash != rec_hash:
                    typer.echo(
                        "Note: Sim or fee plugin code hash differs from recorded paper run; "
                        "fills and charges can legitimately differ."
                    )
                    break
    except Exception:
        pass


@backtest_app.command("run")
@handle_cli_errors
def backtest_run_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Run configuration name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Verify configuration and plan"),
    seed: int | None = typer.Option(None, "--seed", help="Random seed for simulation"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Disable console status lines"),
    fidelity: str = typer.Option("bar", "--fidelity", help="Execution fidelity (bar or tick)"),
    session: str | None = typer.Option(
        None, "--session", help="Recorded session ID for replay or tick fidelity"
    ),
) -> None:
    """Execute a backtest session using composed SessionRunner and cached bars."""
    with _open_rt(ctx, ensure=True) as rt:
        eff = build_effective(rt.root, run_name=name)
        if eff.run is None:
            raise ConfigError(
                what=f"Run configuration '{name}' not found",
                where=f"config/runs/{name}.toml",
                fix=f"Verify configuration file exists for '{name}'",
            )

        if eff.run.run.mode != Mode.BACKTEST:
            raise ConfigError(
                what=(
                    f"Run configuration '{name}' is in mode '{eff.run.run.mode}', "
                    "expected 'backtest'"
                ),
                where=f"config/runs/{name}.toml",
                fix="Set run.mode = 'backtest' in the run configuration",
            )

        if fidelity.lower() == "tick":
            if not session:
                raise ConfigError(
                    what="Tick fidelity backtest requires --session <paper_run_id>",
                    where="backtest.run",
                    fix="Specify --session <paper_run_id> to run with tick fidelity",
                )
            sess_dir = rt.paths.results / session
            if not sess_dir.is_dir() and Path(session).is_dir():
                sess_dir = Path(session)
            if not sess_dir.is_dir():
                raise ConfigError(
                    what=f"Session directory not found for '{session}'",
                    where=f"results/{session}",
                    fix="Provide a valid paper session ID",
                )

            from algodesk.engine.replay_config import (
                apply_recorded_registry_snapshot,
                load_recorded_session_config,
            )

            run_config, session_warnings = load_recorded_session_config(sess_dir, rt)
            for w in session_warnings:
                typer.echo(f"Warning: {w}")

            _check_sim_fee_hashes(rt, sess_dir, run_config)

            snap_file = sess_dir / "registry.snapshot.json"
            if snap_file.is_file() and rt.params is not None:
                apply_recorded_registry_snapshot(snap_file, rt.params)

            runner = BacktestRunner(
                runtime=rt,
                run_config=run_config,
                run_name=name,
                seed=seed,
                dry_run=dry_run,
                quiet=quiet,
                fidelity="tick",
                replay_session=session,
            )

            try:
                res_path = runner.run()
                if getattr(runner.session_runner, "_interrupted", False):
                    typer.echo(f"Backtest session stopped: {res_path}")
                else:
                    typer.echo(f"Backtest session completed: {res_path}")
            except KeyboardInterrupt:
                res_path = rt.paths.results / runner.run_id
                typer.echo(f"\nBacktest session stopped: {res_path}")
            return

        # 1. Evaluate readiness before backtest run
        report, effective_run = evaluate_backtest_readiness(eff.run, rt)

        fee_report = check_fees_readiness(eff.run, rt)
        if fee_report.has_errors:
            raise ConfigError(
                what=fee_report.error_message,
                where=f"backtest.run:{name}",
                fix=fee_report.remediation_fix,
            )

        allowed_missing = _resolve_missing_allowance(eff.run, rt)

        if not dry_run and report.missing_pct > allowed_missing:
            _print_readiness_report(report, allowed_missing)
            fix_msg = (
                report.remediation_commands[0]
                if report.remediation_commands
                else "Accumulate missing data"
            )
            raise ConfigError(
                what=(
                    f"Readiness check failed: missing share {report.missing_pct:.1f}% "
                    f"exceeds backtest.max_missing_pct ({allowed_missing:.1f}%)"
                ),
                where=f"backtest.run:{name}",
                fix=fix_msg,
            )

        # 2. Inform about skipped symbols below the limit
        if report.skipped and not quiet:
            typer.echo(
                f"Skipping {len(report.skipped)} symbol(s) below missing threshold "
                f"({report.missing_pct:.1f}% <= {allowed_missing:.1f}%):"
            )
            for sk in report.skipped:
                typer.echo(f"  - {sk['symbol']}: {sk['reason']}")

        # 3. Configure FeeEngine fallback and reset counter
        fees_fallback = getattr(eff.run.fees, "fallback", None)
        FeeEngine.set_fallback(fees_fallback)
        FeeEngine.reset_fallback_count()

        # 4. Instantiate runner with effective universe (engine unchanged)
        runner = BacktestRunner(
            runtime=rt,
            run_config=effective_run,
            run_name=name,
            seed=seed,
            dry_run=dry_run,
            quiet=quiet,
        )

        if dry_run:
            runner.session_runner.run_prestart_doctor()
            cfg = runner.session_runner.run_config
            bt = getattr(cfg, "backtest", None)
            plan_lines = [
                "=== Backtest Dry-Run Execution Plan ===",
                f"Run Name:          {name}",
                "Mode:              backtest",
                f"Fidelity:          {getattr(bt, 'fidelity', 'bar')}",
                f"Timeframe:         {getattr(bt, 'timeframe', '1d')}",
                f"Window:            {getattr(bt, 'start', '')} to {getattr(bt, 'end', '')}",
                f"Allocated Capital: ₹{cfg.allocated_capital:,.2f}" if cfg else "",
                (
                    f"Effective Universe:{len(report.with_data)} symbols "
                    f"({len(report.skipped)} skipped)"
                ),
                "Doctor checks:     0 critical",
                "Plan verified successfully. No network connections opened; no live orders placed.",
            ]
            for pl in plan_lines:
                if pl:
                    typer.echo(pl)
            return

        try:
            res_path = runner.run()

            # 5. Write readiness.json to results folder
            if res_path and Path(res_path).is_dir():
                readiness_path = Path(res_path) / "readiness.json"
                readiness_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

                # 6. Record fee fallback into metrics.json and SUMMARY.md if used
                fb_used = FeeEngine.get_fallback_used()
                if fb_used > 0:
                    metrics_file = Path(res_path) / "metrics.json"
                    if metrics_file.is_file():
                        try:
                            m_data = json.loads(metrics_file.read_text(encoding="utf-8"))
                            m_data["fees.fallback_used"] = fb_used
                            metrics_file.write_text(json.dumps(m_data, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                    summary_file = Path(res_path) / "SUMMARY.md"
                    if summary_file.is_file():
                        try:
                            s_text = summary_file.read_text(encoding="utf-8")
                            caveat = (
                                f"Fee caveat: earliest verified rates used "
                                f"({fb_used} fallback calculations).\n"
                            )
                            if "Fee caveat" not in s_text:
                                s_text = s_text + "\n" + caveat
                                summary_file.write_text(s_text, encoding="utf-8")
                        except Exception:
                            pass

            if getattr(runner.session_runner, "_interrupted", False):
                typer.echo(f"Backtest session stopped: {res_path}")
            else:
                typer.echo(f"Backtest session completed: {res_path}")
        except KeyboardInterrupt:
            res_path = rt.paths.results / runner.run_id
            readiness_path = Path(res_path) / "readiness.json"
            if res_path.is_dir():
                readiness_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
            typer.echo(f"\nBacktest session stopped: {res_path}")


@backtest_app.command("equivalence")
@handle_cli_errors
def backtest_equivalence_cmd(
    ctx: typer.Context,
    session: str = typer.Option(..., "--session", help="Recorded paper session ID"),
) -> None:
    """Verify equivalence between paper session signals and reconstructed bar backtest."""
    from algodesk.engine.equivalence import run_backtest_equivalence

    with _open_rt(ctx, ensure=True) as rt:
        res = run_backtest_equivalence(rt, session)
        typer.echo(f"signals equal: {res.signals_equal}")
        typer.echo(
            f"Paper signals: {res.paper_signals_count}, "
            f"Backtest signals: {res.backtest_signals_count}"
        )
        if res.only_in_paper:
            typer.echo(f"Only in paper ({len(res.only_in_paper)}):")
            for item in res.only_in_paper:
                typer.echo(f"  {item.get('formatted', str(item))}")
        if res.only_in_backtest:
            typer.echo(f"Only in backtest ({len(res.only_in_backtest)}):")
            for item in res.only_in_backtest:
                typer.echo(f"  {item.get('formatted', str(item))}")
        if (
            not res.signals_equal
            and not res.only_in_paper
            and not res.only_in_backtest
            and res.differences
        ):
            typer.echo("Differences:")
            for d in res.differences:
                typer.echo(f"  {d.index + 1}. [{d.classification}] {d.description}")
        typer.echo(
            f"Paper orders: {res.paper_orders_count}, Backtest orders: {res.backtest_orders_count}"
        )
        typer.echo(
            f"Paper fills: {res.paper_fills_count}, Backtest fills: {res.backtest_fills_count}"
        )

        if not res.signals_equal:
            raise typer.Exit(code=1)


@backtest_app.command("calibrate")
@handle_cli_errors
def backtest_calibrate_cmd(
    ctx: typer.Context,
    session: str = typer.Option(..., "--session", help="Recorded paper session ID"),
) -> None:
    """Calibrate backtest slippage against tick-fidelity paper fills."""
    from algodesk.engine.calibration import run_slippage_calibration

    with _open_rt(ctx, ensure=True) as rt:
        res = run_slippage_calibration(rt, session)
        if res.refused:
            typer.echo(f"=== Slippage Calibration: {res.session_id} ===")
            typer.echo(f"Refused: {res.refusal_reason}")
            typer.echo(f"Fills compared: {res.fills_compared}")
            typer.echo(f"Unpaired paper fills: {res.unpaired_paper_fills}")
            typer.echo(f"Unpaired backtest fills: {res.unpaired_backtest_fills}")
            return

        typer.echo(f"=== Slippage Calibration: {res.session_id} ===")
        typer.echo(f"Fills compared: {res.fills_compared}")
        typer.echo(f"Adverse pairs count: {res.adverse_pairs_count}")
        typer.echo(
            f"Unpaired fills: {res.unpaired_paper_fills} paper, "
            f"{res.unpaired_backtest_fills} backtest"
        )
        typer.echo("Adverse slippage (bps):")
        typer.echo(f"  Mean:   {res.mean_adverse_bps:.2f} bps")
        typer.echo(f"  Median: {res.median_adverse_bps:.2f} bps")
        typer.echo(f"  95th %: {res.p95_adverse_bps:.2f} bps")
        typer.echo("Net P&L difference per trade:")
        typer.echo(f"  Mean:   ₹{res.mean_pnl_diff:.2f}")
        typer.echo(f"  Median: ₹{res.median_pnl_diff:.2f}")
        typer.echo(f"\nSUGGESTED backtest.slippage_bps: {res.suggested_slippage_bps} (advisory)")
        typer.echo(f"Wrote: {res.calibration_file}")
        typer.echo("Note: Parameter registry was not changed.")


@backtest_app.command("benchmark")
@handle_cli_errors
def backtest_benchmark_cmd(
    ctx: typer.Context,
    symbols: int = typer.Option(100, "--symbols", help="Number of symbols"),
    sessions: int = typer.Option(250, "--sessions", help="Number of sessions"),
    timeframe: str = typer.Option("1d", "--timeframe", help="Timeframe (1d or 1m)"),
) -> None:
    """Benchmark full runner execution speed, throughput, and memory with cProfile."""
    from algodesk.engine.benchmark import run_benchmark_measurement

    rep = run_benchmark_measurement(
        symbols_count=symbols,
        sessions_count=sessions,
        timeframe=timeframe,
    )
    typer.echo("=== Backtest Benchmark ===")
    typer.echo(f"Configuration: {rep.symbols} symbols x {rep.sessions} sessions ({rep.timeframe})")
    typer.echo(f"Total bars: {rep.total_bars:,}")
    typer.echo(f"Elapsed time: {rep.elapsed_seconds:.2f}s")
    typer.echo(f"Throughput: {rep.bars_per_second:,.0f} bars/s")
    typer.echo(f"Fixed cost: {rep.fixed_cost_seconds:.3f}s")
    per_bar_us = rep.per_bar_cost_seconds * 1_000_000
    typer.echo(f"Per-bar cost: {per_bar_us:.1f} µs/bar ({rep.per_bar_cost_seconds:.6f} s/bar)")
    typer.echo(f"Peak memory: {rep.peak_memory_mb:.1f} MB")
    typer.echo("\nTop 10 functions by internal time (tottime):")
    typer.echo(rep.profile_stats_text.strip())
