"""Secret management and scanning CLI commands."""

from __future__ import annotations

import getpass
import sys
from typing import Any

import typer

from algodesk.api.kinds import Mode
from algodesk.cli.common import _get_cli_state, _open_rt, handle_cli_errors
from algodesk.ops.context import build_context

secrets_app = typer.Typer(help="Scan codebase for exposed secrets and tokens.")


@secrets_app.command("scan")
@handle_cli_errors
def secrets_scan_cmd(
    ctx: typer.Context,
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Scan codebase for exposed secrets, tokens, and cloud keys."""
    from algodesk.devtools.secretscan import (
        format_secretscan_json,
        format_secretscan_text,
        run_secretscan,
    )

    state = _get_cli_state(ctx)
    is_json = json_opt or state.json
    with _open_rt(ctx, ensure=False) as rt:
        rep = run_secretscan(rt.root)

    if is_json:
        typer.echo(format_secretscan_json(rep))
    else:
        typer.echo(format_secretscan_text(rep))

    if rep.exit_code != 0:
        raise typer.Exit(code=rep.exit_code)


def _resolve_secrets_provider(
    rt: Any,
    provider_name: str | None = None,
    run: str | None = None,
) -> Any:
    p_name = provider_name
    if not p_name and run:
        from algodesk.config.loader import load_toml

        run_path = rt.root / "config" / "runs" / f"{run}.toml"
        if run_path.is_file():
            try:
                rc = load_toml(run_path)
                sec_dict = rc.get("secrets", {})
                if isinstance(sec_dict, dict) and "plugin" in sec_dict:
                    p_name = str(sec_dict["plugin"])
            except Exception:
                pass

    if not p_name:
        if rt.effective.run is not None and getattr(rt.effective.run, "secrets", None) is not None:
            p_name = rt.effective.run.secrets.plugin
        elif sys.platform == "darwin":
            p_name = "keychain"
        else:
            p_name = "env"

    p_ctx = build_context(Mode.RESEARCH, run_id="secrets_cli")
    return rt.plugins.create("secrets", p_name, config={}, ctx=p_ctx)


@secrets_app.command("set")
@handle_cli_errors
def secrets_set_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Secret name"),
    use_stdin: bool = typer.Option(False, "--stdin", help="Read secret value from stdin"),
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
    provider: str | None = typer.Option(None, "--provider", help="Secrets provider name"),
) -> None:
    """Store a secret securely in the configured secrets provider."""
    if use_stdin:
        val = sys.stdin.read().strip()
    else:
        val = getpass.getpass(f"Enter secret value for '{name}': ").strip()

    if not val:
        raise typer.BadParameter("Secret value cannot be empty")

    with _open_rt(ctx, ensure=False) as rt:
        prov = _resolve_secrets_provider(rt, provider_name=provider, run=run)
        prov.set(name, val)
    typer.echo(f"Secret '{name}' stored successfully.")


@secrets_app.command("list")
@handle_cli_errors
def secrets_list_cmd(
    ctx: typer.Context,
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
    provider: str | None = typer.Option(None, "--provider", help="Secrets provider name"),
) -> None:
    """List stored secret names (names only, values never displayed)."""
    with _open_rt(ctx, ensure=False) as rt:
        prov = _resolve_secrets_provider(rt, provider_name=provider, run=run)
        names = prov.names()
    for n in sorted(names):
        typer.echo(n)


@secrets_app.command("delete")
@handle_cli_errors
def secrets_delete_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Secret name to delete"),
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
    provider: str | None = typer.Option(None, "--provider", help="Secrets provider name"),
) -> None:
    """Delete a secret from the configured secrets provider."""
    with _open_rt(ctx, ensure=False) as rt:
        prov = _resolve_secrets_provider(rt, provider_name=provider, run=run)
        prov.delete(name)
    typer.echo(f"Secret '{name}' deleted.")
