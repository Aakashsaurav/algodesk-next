"""Parameter and configuration CLI commands."""

from __future__ import annotations

import getpass
import json

import typer

from algodesk.api.errors import ParamError
from algodesk.cli import common
from algodesk.cli.common import _get_cli_state, format_table, handle_cli_errors
from algodesk.config.snapshot import render_effective
from algodesk.ops.runtime import Runtime
from algodesk.params.specs import parse_cli_value


def _open_rt(ctx: typer.Context, *, ensure: bool = False) -> Runtime:
    import sys

    app_mod = sys.modules.get("algodesk.cli.app")
    open_fn = getattr(app_mod, "_open_rt", common._open_rt)
    if open_fn is not _open_rt:
        return open_fn(ctx, ensure=ensure)
    return common._open_rt(ctx, ensure=ensure)


param_app = typer.Typer(help="Inspect and modify parameters in the parameter registry.")
config_app = typer.Typer(help="Inspect effective platform and run configuration.")


@config_app.command("show")
@handle_cli_errors
def config_show(ctx: typer.Context) -> None:
    """Show effective configuration snapshot."""
    state = _get_cli_state(ctx)
    with _open_rt(ctx, ensure=False) as rt:
        if state.json:
            typer.echo(json.dumps(rt.effective.platform.model_dump(mode="json"), indent=2))
        else:
            typer.echo(render_effective(rt.effective))


@param_app.command("show")
@handle_cli_errors
def param_show(
    ctx: typer.Context,
    prefix: str = typer.Argument("", help="Parameter prefix to filter"),
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Show registered parameters and their effective values."""
    state = _get_cli_state(ctx)
    is_json = json_opt or state.json
    with _open_rt(ctx, ensure=False) as rt:
        if rt.params is not None:
            rows = rt.params.show(prefix)
        else:
            from algodesk.params.registry import ParamRegistry

            mem_reg = ParamRegistry(":memory:")
            defaults_dir = rt.root / "config" / "params"
            if defaults_dir.is_dir():
                mem_reg.load_defaults(defaults_dir)
            rows = mem_reg.show(prefix)

    if is_json:
        data = [
            {
                "name": r.name,
                "value": r.value,
                "default": r.default,
                "scope": r.scope,
                "last_change": r.last_change,
            }
            for r in rows
        ]
        typer.echo(json.dumps(data, indent=2))
    else:
        headers = ["NAME", "VALUE", "DEFAULT", "SCOPE", "LAST_CHANGE"]
        table_rows = [
            [r.name, str(r.value), str(r.default), r.scope, str(r.last_change or "-")] for r in rows
        ]
        typer.echo(format_table(headers, table_rows))


@param_app.command("set")
@handle_cli_errors
def param_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Parameter name"),
    value: str = typer.Argument(..., help="New parameter value"),
    reason: str = typer.Option(..., "--reason", "-r", help="Mandatory reason for change"),
    scope: str = typer.Option("global", "--scope", "-s", help="Parameter scope"),
    confirm: str | None = typer.Option(None, "--confirm", help="Confirmation phrase for loosen"),
) -> None:
    """Set parameter value override with audit logging."""
    if not reason or not reason.strip():
        raise typer.BadParameter("Reason must not be empty", param_hint="--reason")

    with _open_rt(ctx, ensure=True) as rt:
        if rt.params is None:
            raise ParamError(
                what="Parameter registry database could not be opened",
                where=name,
                fix="Check database permissions in data/state/params.db",
            )
        try:
            spec = rt.params.spec(name)
        except ParamError as exc:
            raise ParamError(
                what=f"Cannot operate on unknown parameter '{name}'",
                where=name,
                fix="Register parameter spec before operating on it",
            ) from exc
        parsed_val = parse_cli_value(spec, value)
        by = getpass.getuser()
        res = rt.params.set(
            name,
            parsed_val,
            reason=reason,
            scope=scope,
            confirm=confirm,
            by=by,
            channel="cli",
        )
        typer.echo(f"OK: {res.name} -> {res.new_value} ({res.direction}, {res.status})")


@param_app.command("history")
@handle_cli_errors
def param_history(
    ctx: typer.Context,
    name: str | None = typer.Argument(None, help="Optional parameter name"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum history records"),
) -> None:
    """Show parameter audit change history."""
    with _open_rt(ctx, ensure=False) as rt:
        if rt.params is None:
            typer.echo("Parameter database not initialized.")
            return
        entries = rt.params.history(name=name, limit=limit)

    if not entries:
        typer.echo("No parameter history records found.")
        return

    headers = ["TS_UTC", "NAME", "SCOPE", "OLD_VAL", "NEW_VAL", "BY", "STATUS", "REASON"]
    rows = [
        [e.ts_utc, e.name, e.scope, str(e.old_value), str(e.new_value), e.by, e.status, e.reason]
        for e in entries
    ]
    typer.echo(format_table(headers, rows))


@param_app.command("reset")
@handle_cli_errors
def param_reset(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Parameter name to reset"),
    reason: str = typer.Option(..., "--reason", "-r", help="Mandatory reason for reset"),
    scope: str = typer.Option("global", "--scope", "-s", help="Parameter scope"),
) -> None:
    """Reset parameter override back to default specification."""
    if not reason or not reason.strip():
        raise typer.BadParameter("Reason must not be empty", param_hint="--reason")

    with _open_rt(ctx, ensure=True) as rt:
        if rt.params is None:
            raise ParamError(
                what="Parameter registry database could not be opened",
                where=name,
                fix="Check database permissions in data/state/params.db",
            )
        by = getpass.getuser()
        res = rt.params.reset(name, reason=reason, scope=scope, by=by, channel="cli")
        typer.echo(f"OK: reset {res.name} -> {res.new_value} ({res.direction}, {res.status})")
