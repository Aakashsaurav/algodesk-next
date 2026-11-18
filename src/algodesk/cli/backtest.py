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

    if report.remediation_commands:
        typer.echo("\nRemediation commands:")
        for cmd in report.remediation_commands:
            typer.echo(f"  {cmd}")


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

        allowed_missing = _resolve_missing_allowance(eff.run, rt)

        if json_output:
            typer.echo(json.dumps(report.to_dict(), indent=2))
        else:
            _print_readiness_report(report, allowed_missing)

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


@backtest_app.command("run")
@handle_cli_errors
def backtest_run_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Run configuration name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Verify configuration and plan"),
    seed: int | None = typer.Option(None, "--seed", help="Random seed for simulation"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Disable console status lines"),
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

        # 1. Evaluate readiness before backtest run
        report, effective_run = evaluate_backtest_readiness(eff.run, rt)

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
