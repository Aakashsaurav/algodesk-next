"""AlgoDesk command-line interface wiring and entrypoints."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Annotated, Any

import click
import typer

from algodesk.api.errors import AlgoDeskError
from algodesk.cli import common
from algodesk.cli.common import (
    CliState,
    _get_cli_state,
    format_table,
    handle_cli_errors,
)
from algodesk.cli.data import data_app, groups_app
from algodesk.cli.doctor_devtools import (
    api_app,
    budget_cmd,
    docs_app,
    doctor_cmd,
    lint_cmd,
)
from algodesk.cli.fees_calendar import calendar_app, fees_app
from algodesk.cli.instruments import instruments_app
from algodesk.cli.journal import journal_app
from algodesk.cli.login_feed import feed_app, login_app
from algodesk.cli.param import config_app, param_app
from algodesk.cli.plugins import new_app, plugins_app
from algodesk.cli.risk import killswitch_app
from algodesk.cli.run_replay import replay_cmd, results_app, run_cmd
from algodesk.cli.secrets import secrets_app
from algodesk.cli.session import session_app
from algodesk.cli.spike import spike_app
from algodesk.cli.telegram import telegram_app
from algodesk.ops.checks import build_core_checks
from algodesk.ops.fees import inspect_fee_schedule
from algodesk.ops.runtime import Runtime


def _render_fees_show(info: dict[str, Any], json_output: bool) -> None:
    schedule, eval_date = info["schedule"], info["as_of"]
    active_comps, active_taxes = info["active_comps"], info["active_taxes"]
    if json_output:
        import json

        data = {
            "schedule": schedule,
            "as_of": eval_date.isoformat(),
            "components": [
                {
                    "name": c.name,
                    "segment": c.segment,
                    "product": c.product,
                    "kind": c.kind,
                    "rate": str(c.rate) if c.rate is not None else None,
                    "amount": str(c.amount) if c.amount is not None else None,
                    "side": c.side,
                    "exchange": getattr(c, "exchange", None),
                    "instrument_type": getattr(c, "instrument_type", None),
                    "effective_from": c.effective_from.isoformat(),
                    "effective_to": c.effective_to.isoformat() if c.effective_to else None,
                }
                for c in active_comps
            ],
            "taxes": [
                {"name": t.name, "rate": str(t.rate), "applies_to": t.applies_to}
                for t in active_taxes
            ],
        }
        typer.echo(json.dumps(data, indent=2))
        return

    typer.echo(f"=== Fee Schedule: {schedule} (As-of: {eval_date.isoformat()}) ===")
    meta_ver = getattr(info["meta"], "verified_on", "unverified")
    typer.echo(f"Source: {info['meta'].source}\nVerified on: {meta_ver}\n")
    headers = [
        "Segment",
        "Product",
        "Component",
        "Side",
        "Kind",
        "Rate / Amount",
        "Exch",
        "Type",
        "From",
    ]
    rows = []
    for c in sorted(active_comps, key=lambda x: (x.segment, x.product, x.name)):
        val = (
            f"{c.rate}"
            if c.rate is not None
            else (f"₹{c.amount:.2f}" if c.amount is not None else "")
        )
        rows.append(
            [
                c.segment.upper(),
                c.product.upper(),
                c.name,
                c.side,
                c.kind,
                val,
                getattr(c, "exchange", None) or "all",
                getattr(c, "instrument_type", None) or "all",
                c.effective_from.isoformat(),
            ]
        )
    typer.echo(format_table(headers, rows))
    if active_taxes:
        typer.echo("\n--- Applicable Taxes ---")
        for t in active_taxes:
            applies = ", ".join(t.applies_to)
            typer.echo(f"  {t.name.upper()}: {float(t.rate) * 100:.1f}% on {applies}")


@fees_app.command("show")
@handle_cli_errors
def fees_show_cmd(
    ctx: typer.Context,
    schedule: str = typer.Argument(..., help="Fee schedule name (e.g. zerodha_nse)"),
    as_of: str | None = typer.Option(
        None, "--as-of", help="Date YYYY-MM-DD to evaluate applicable components"
    ),
    segment: str | None = typer.Option(None, "--segment", help="Filter by segment (eq, fno)"),
    product: str | None = typer.Option(
        None, "--product", help="Filter by product (CNC, MIS, NRML)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output results in JSON format"),
) -> None:
    """Show applicable fee components and rates for a schedule on a given date."""
    with _open_rt(ctx) as rt:
        res = inspect_fee_schedule(
            rt=rt, schedule=schedule, as_of=as_of, segment=segment, product=product
        )
        _render_fees_show(res, json_output=json_output)


def _open_rt(ctx: typer.Context, *, ensure: bool = False) -> Runtime:
    return common._open_rt(ctx, ensure=ensure)


app = typer.Typer(
    name="algodesk",
    help="AlgoDesk Next — pluggable trading workbench for Indian markets.",
    no_args_is_help=True,
)

# Mount command groups
app.add_typer(param_app, name="param")
app.add_typer(config_app, name="config")
app.add_typer(plugins_app, name="plugins")
app.add_typer(new_app, name="new")
app.add_typer(journal_app, name="journal")
app.add_typer(secrets_app, name="secrets")
app.add_typer(calendar_app, name="calendar")
app.add_typer(fees_app, name="fees")
app.add_typer(data_app, name="data")
app.add_typer(groups_app, name="groups")
app.add_typer(instruments_app, name="instruments")
app.add_typer(login_app, name="login")
app.add_typer(feed_app, name="feed")
app.add_typer(docs_app, name="docs")
app.add_typer(api_app, name="api")
app.add_typer(killswitch_app, name="killswitch")
app.add_typer(results_app, name="results")
app.add_typer(session_app, name="session")
app.add_typer(telegram_app, name="telegram")
app.add_typer(spike_app, name="spike")
app.add_typer(
    __import__("algodesk.cli.indicators", fromlist=["indicators_app"]).indicators_app,
    name="indicators",
)
app.add_typer(
    __import__("algodesk.cli.screen", fromlist=["screen_app"]).screen_app,
    name="screen",
)
app.add_typer(
    __import__("algodesk.cli.backtest", fromlist=["backtest_app"]).backtest_app,
    name="backtest",
)

# Register top-level commands
app.command("doctor")(doctor_cmd)
app.command("budget")(budget_cmd)
app.command("lint")(lint_cmd)
app.command("run")(run_cmd)
app.command("replay")(replay_cmd)


@app.callback()
def main_callback(
    ctx: typer.Context,
    root: Annotated[Path | None, typer.Option("--root", help="Workspace root directory")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Configuration profile")] = None,
    run: Annotated[str | None, typer.Option("--run", help="Run configuration name")] = None,
    json_opt: Annotated[bool, typer.Option("--json", help="Machine-readable JSON output")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Print debug traceback on errors")] = False,
) -> None:
    """AlgoDesk command-line workbench."""
    ctx.obj = CliState(root=root, profile=profile, run=run, json=json_opt, debug=debug)


@app.command("version")
@handle_cli_errors
def version_cmd() -> None:
    """Print AlgoDesk version."""
    typer.echo("algodesk 0.0.0")


def main() -> None:
    """Top-level entrypoint for the algodesk CLI."""
    debug = "--debug" in sys.argv
    try:
        app()
    except typer.Exit as e:
        sys.exit(e.exit_code)
    except (typer.BadParameter, click.UsageError, click.exceptions.ClickException):
        sys.exit(2)
    except AlgoDeskError as err:
        if debug:
            traceback.print_exc()
        typer.echo(f"ERROR: {err.what}")
        typer.echo(f"WHERE: {err.where}")
        typer.echo(f"FIX: {err.fix}")
        sys.exit(1)
    except Exception as exc:
        if debug:
            traceback.print_exc()
        typer.echo(f"INTERNAL ERROR (bug): {type(exc).__name__}: {exc}")
        typer.echo("run with --debug for the traceback")
        sys.exit(70)


__all__ = [
    "CliState",
    "_get_cli_state",
    "_open_rt",
    "app",
    "build_core_checks",
    "format_table",
    "handle_cli_errors",
    "main",
]
