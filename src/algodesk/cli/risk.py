"""CLI commands for managing killswitch and risk controls."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from algodesk.cli.common import handle_cli_errors
from algodesk.ops.paths import find_project_root
from algodesk.risk.killswitch import KillSwitch

killswitch_app = typer.Typer(help="Inspect and manage emergency trading kill-switch.")


def _get_killswitch(ctx: typer.Context) -> KillSwitch:
    root = getattr(ctx.obj, "root", None) or find_project_root()
    state_file = Path(root) / "data" / "state" / "killswitch.json"
    return KillSwitch(state_file=state_file)


@killswitch_app.command("on")
@handle_cli_errors
def killswitch_on(
    ctx: typer.Context,
    reason: str = typer.Option(..., "--reason", "-r", help="Mandatory reason for tripping"),
) -> None:
    """Trip the kill-switch to block all order submissions."""
    ks = _get_killswitch(ctx)
    ks.trip(reason=reason, by="cli")
    typer.echo(f"Kill-switch ACTIVATED: {reason}")


@killswitch_app.command("off")
@handle_cli_errors
def killswitch_off(
    ctx: typer.Context,
    reason: str = typer.Option(..., "--reason", "-r", help="Mandatory reason for clearing"),
) -> None:
    """Clear the kill-switch to permit order submissions."""
    ks = _get_killswitch(ctx)
    ks.clear(reason=reason, by="cli")
    typer.echo(f"Kill-switch CLEARED: {reason}")


@killswitch_app.command("status")
@handle_cli_errors
def killswitch_status(ctx: typer.Context) -> None:
    """Show current kill-switch status."""
    ks = _get_killswitch(ctx)
    info = ks.status()
    json_opt = getattr(ctx.obj, "json", False)

    if json_opt:
        typer.echo(json.dumps(info, indent=2))
        return

    if info.get("active"):
        reason = info.get("reason")
        by = info.get("by")
        ts = info.get("ts")
        typer.echo(f"Kill-switch: ACTIVE (reason: {reason}, by: {by}, at: {ts})")
    else:
        reason = info.get("reason")
        ts = info.get("ts")
        typer.echo(f"Kill-switch: CLEARED (last reason: {reason}, at: {ts})")
