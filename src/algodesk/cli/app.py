"""AlgoDesk command-line interface wiring and entrypoints."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Annotated

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
from algodesk.cli.journal import journal_app
from algodesk.cli.login_feed import feed_app, login_app
from algodesk.cli.param import config_app, param_app
from algodesk.cli.plugins import new_app, plugins_app
from algodesk.cli.risk import killswitch_app
from algodesk.cli.run_replay import replay_cmd, results_app, run_cmd
from algodesk.cli.secrets import secrets_app
from algodesk.cli.telegram import telegram_app
from algodesk.ops.checks import build_core_checks
from algodesk.ops.runtime import Runtime


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
app.add_typer(login_app, name="login")
app.add_typer(feed_app, name="feed")
app.add_typer(docs_app, name="docs")
app.add_typer(api_app, name="api")
app.add_typer(killswitch_app, name="killswitch")
app.add_typer(results_app, name="results")
app.add_typer(telegram_app, name="telegram")

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
