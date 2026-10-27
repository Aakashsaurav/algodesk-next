"""Append-only journal inspection, tail, and verification CLI commands."""

from __future__ import annotations

from typing import Any

import typer

from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.journal.store import runs, tail, verify_journal

journal_app = typer.Typer(help="Inspect, tail, and verify append-only journal.")


@journal_app.command("verify")
@handle_cli_errors
def journal_verify(ctx: typer.Context) -> None:
    """Verify hash chain integrity of append-only journal."""
    with _open_rt(ctx, ensure=False) as rt:
        db_path = rt.state.journal_db
        if not db_path.exists():
            typer.echo("Journal database does not exist.")
            return
        res = verify_journal(db_path)

    if res.ok:
        typer.echo(f"JOURNAL OK: verified {res.checked} events")
    else:
        typer.echo(f"JOURNAL CORRUPT: {res.reason} (bad sequence {res.first_bad_seq})")
        raise typer.Exit(code=1)


@journal_app.command("tail")
@handle_cli_errors
def journal_tail(
    ctx: typer.Context,
    n: int = typer.Option(10, "-n", help="Number of recent events to display"),
) -> None:
    """Display most recent journal events."""
    with _open_rt(ctx, ensure=False) as rt:
        db_path = rt.state.journal_db
        if not db_path.exists():
            typer.echo("Journal database does not exist.")
            return
        event_list = tail(db_path, n=n)

    if not event_list:
        typer.echo("No journal events found.")
        return

    headers = ["SEQ", "TS_UTC", "KIND", "RUN_ID"]
    rows = [
        [
            str(e.get("seq", "")),
            str(e.get("ts_utc", "")),
            str(e.get("kind", "")),
            str(e.get("run_id", "")),
        ]
        for e in event_list
    ]
    typer.echo(format_table(headers, rows))


@journal_app.command("runs")
@handle_cli_errors
def journal_runs(ctx: typer.Context) -> None:
    """List execution runs recorded in the journal."""
    with _open_rt(ctx, ensure=False) as rt:
        db_path = rt.state.journal_db
        if not db_path.exists():
            typer.echo("Journal database does not exist.")
            return

        raw_runs: list[dict[str, Any]] = []
        try:
            raw_runs = runs(db_path)
        except Exception:
            raw_runs = []

    if not raw_runs:
        typer.echo("No runs found in journal.")
        return

    headers = ["RUN_ID", "MODE", "STATUS", "STARTED_UTC", "ENDED_UTC"]
    rows = [
        [
            str(r.get("run_id", "")),
            str(r.get("mode", "")),
            str(r.get("status", "")),
            str(r.get("started_utc", "")),
            str(r.get("ended_utc") or "-"),
        ]
        for r in raw_runs
    ]
    typer.echo(format_table(headers, rows))
