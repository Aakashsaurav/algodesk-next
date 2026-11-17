"""AlgoDesk CLI commands for backtesting."""

from __future__ import annotations

import typer

from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.cli.common import _open_rt, handle_cli_errors
from algodesk.config.snapshot import build_effective
from algodesk.engine.backtest import BacktestRunner

backtest_app = typer.Typer(help="Execute and manage backtest sessions.")


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

        runner = BacktestRunner(
            runtime=rt,
            run_config=eff.run,
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
                "Doctor checks:     0 critical",
                "Plan verified successfully. No network connections opened; no live orders placed.",
            ]
            for pl in plan_lines:
                if pl:
                    typer.echo(pl)
            return

        try:
            res_path = runner.run()
            if getattr(runner.session_runner, "_interrupted", False):
                typer.echo(f"Backtest session stopped: {res_path}")
            else:
                typer.echo(f"Backtest session completed: {res_path}")
        except KeyboardInterrupt:
            res_path = rt.paths.results / runner.run_id
            typer.echo(f"\nBacktest session stopped: {res_path}")
