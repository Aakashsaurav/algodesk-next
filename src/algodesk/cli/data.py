"""Market data inspection, retrieval, deletion, and trash CLI commands."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import typer

from algodesk.api import InstrumentId, SeriesKey
from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.data.catalog import (
    coverage,
    execute_delete,
    list_trash,
    plan_delete,
    purge_trash,
)
from algodesk.data.groups import get_group, list_group_details
from algodesk.data.manifest import DataManifest
from algodesk.data.recorder import MarketRecorder
from algodesk.data.synth import synth_ticks
from algodesk.ops.context import build_context
from algodesk.ops.runtime import Runtime

data_app = typer.Typer(help="Fetch, inspect, delete, and manage market data.")
data_trash_app = typer.Typer(help="Manage data safety trash.")
groups_app = typer.Typer(help="Inspect and list configured instrument groups.")

data_app.add_typer(data_trash_app, name="trash")


def _parse_data_cli_dt(val: str, is_end: bool) -> datetime:
    val = val.strip()
    ist = ZoneInfo("Asia/Kolkata")
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        d = date.fromisoformat(val)
        if is_end:
            return datetime.combine(d, time(23, 59, 59), tzinfo=ist).astimezone(UTC)
        return datetime.combine(d, time.min, tzinfo=ist).astimezone(UTC)


@data_app.command("get")
@handle_cli_errors
def data_get(
    ctx: typer.Context,
    symbol: str = typer.Argument(..., help="Symbol (e.g. RELIANCE, INFY)"),
    timeframe: str = typer.Argument(..., help="Timeframe (e.g. 1m, 5m, 15m, 30m, 1h, 1d)"),
    start: str = typer.Argument(..., help="Start date/time (e.g. YYYY-MM-DD or ISO8601)"),
    end: str = typer.Argument(..., help="End date/time (e.g. YYYY-MM-DD or ISO8601)"),
    adjusted: bool = typer.Option(False, "--adjusted", help="Use adjusted prices"),
    source: str = typer.Option("yfinance", "--source", help="Data source broker plugin"),
) -> None:
    """Fetch and display historical market data bars in a table."""
    ist = ZoneInfo("Asia/Kolkata")
    start_dt = _parse_data_cli_dt(start, is_end=False)
    end_dt = _parse_data_cli_dt(end, is_end=True)

    inst_id = InstrumentId(symbol if ":" in symbol else f"NSE:EQ:{symbol}")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe=timeframe,
        adjustment="adjusted" if adjusted else "raw",
        source=source,
    )

    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"data_get_{source}")
        broker: Any = rt.plugins.create("broker", source, config={}, ctx=p_ctx)
        bars = list(broker.history(key, start_dt, end_dt))

    if not bars:
        typer.echo(f"No bars returned for {symbol} ({timeframe}) between {start} and {end}.")
        return

    is_daily = timeframe.lower() == "1d"
    headers = ["DATE" if is_daily else "DATE/TIME", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
    rows: list[list[str]] = []
    for b in bars:
        ts_ist = b.start_ts.astimezone(ist)
        d_str = ts_ist.strftime("%Y-%m-%d") if is_daily else ts_ist.strftime("%Y-%m-%d %H:%M")
        rows.append(
            [
                d_str,
                f"{b.open:.2f}",
                f"{b.high:.2f}",
                f"{b.low:.2f}",
                f"{b.close:.2f}",
                str(b.volume),
            ]
        )
    typer.echo(format_table(headers, rows))


def _log_data_deleted(
    rt: Runtime,
    reason: str,
    selectors: Sequence[str],
    instruments: Sequence[str],
    files_count: int,
    total_bytes: int,
    purge: bool,
) -> None:
    try:
        from algodesk.journal.writer import Journal, JournalSettings

        flush_interval = int(rt.params.get("journal.flush_interval_ms")) if rt.params else 50
        max_buffer = int(rt.params.get("journal.max_buffer_events")) if rt.params else 1000
        bp_timeout = int(rt.params.get("journal.backpressure_timeout_ms")) if rt.params else 5000
        settings = JournalSettings(
            flush_interval_ms=flush_interval,
            max_buffer_events=max_buffer,
            backpressure_timeout_ms=bp_timeout,
        )
        with Journal(rt.state.journal_db, settings=settings) as journal:
            journal.start_run(
                mode="research",
                code_version="core",
                config_hash="maint",
                registry_hash="data_delete",
            )
            journal.append(
                "data.deleted",
                {
                    "reason": reason,
                    "selectors": list(selectors),
                    "instruments": list(instruments),
                    "files_count": files_count,
                    "bytes": total_bytes,
                    "purge": purge,
                },
                durable=True,
            )
            journal.end_run("ok")
    except Exception:
        pass


@data_app.command("list")
@handle_cli_errors
def data_list(
    ctx: typer.Context,
    group_by: str = typer.Option(
        "none", "--group-by", help="Grouping: exchange|segment|underlying|group|none"
    ),
    group: str | None = typer.Option(None, "--group", help="Filter by instrument group"),
    kind: str | None = typer.Option(None, "--kind", help="Filter by data kind (ticks, bars)"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List recorded market data coverage across partitions."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest_db = rt.root / "data" / "state" / "data_manifest.db"
        manifest = DataManifest(manifest_db)
        groups_dir = rt.root / "config" / "groups"
        rows = coverage(
            manifest,
            group_by=group_by if group_by != "none" else None,
            filters={"kind": kind} if kind else None,
            groups_dir=groups_dir,
        )

        if group:
            members = get_group(group, groups_dir) or []
            if group_by == "none":
                rows = [r for r in rows if r.target in members]

        if json_out:
            typer.echo(json.dumps([asdict(r) for r in rows], indent=2))
            return

        target_col = group_by.upper() if group_by != "none" else "INSTRUMENT"
        headers = [
            target_col,
            "KIND",
            "TIMEFRAME",
            "FIRST DATE",
            "LAST DATE",
            "DAYS",
            "ROWS",
            "BYTES",
            "SOURCE",
        ]
        table_rows = [
            [
                r.target,
                r.kind,
                r.timeframe,
                r.first_date,
                r.last_date,
                str(r.days),
                str(r.rows),
                str(r.bytes),
                r.source,
            ]
            for r in rows
        ]
        typer.echo(format_table(headers, table_rows))


@data_app.command("delete")
@handle_cli_errors
def data_delete(
    ctx: typer.Context,
    selectors: list[str] = typer.Argument(..., help="Symbol, ID, glob, or @group selector(s)"),
    from_date: str | None = typer.Option(None, "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str | None = typer.Option(None, "--to", help="End date (YYYY-MM-DD)"),
    kind: str | None = typer.Option(None, "--kind", help="Data kind (ticks, bars)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Display deletion plan without deleting"),
    purge: bool = typer.Option(False, "--purge", help="Permanently delete files bypassing trash"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    reason: str = typer.Option(..., "--reason", help="Mandatory reason for audit log"),
) -> None:
    """Delete recorded market data with safe trash and transactional manifest updates."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest_db = rt.root / "data" / "state" / "data_manifest.db"
        manifest = DataManifest(manifest_db)
        groups_dir = rt.root / "config" / "groups"
        data_dir = rt.root / "data"

        plan = plan_delete(
            selectors,
            manifest,
            from_date=from_date,
            to_date=to_date,
            kind=kind,
            groups_dir=groups_dir,
        )

        headers = ["INSTRUMENT", "DATE", "KIND", "ROWS", "BYTES", "PATH"]
        table_rows = [
            [f.instrument, f.date, f.kind, str(f.rows), str(f.bytes), f.path] for f in plan.files
        ]
        if table_rows:
            typer.echo(format_table(headers, table_rows))

        # List @group members or selectors without data as "no data"
        for inst in plan.no_data_instruments:
            typer.echo(f"{inst}: no data")

        typer.echo(
            f"Plan: {plan.total_files} file(s) across {len(plan.instruments)} instrument(s) "
            f"({plan.total_bytes} bytes, {plan.total_rows} rows)"
        )

        if dry_run:
            return

        if plan.total_files == 0:
            typer.echo("No files found matching selector(s).")
            return

        if not yes:
            expected = f"DELETE {plan.total_files} files"
            resp = typer.prompt(f"Type '{expected}' to confirm")
            if resp.strip() != expected:
                typer.echo("Deletion aborted.")
                raise typer.Exit(code=1)

        raw_trash_days = rt.params.get("data.trash_days") if rt.params else None
        trash_days = int(raw_trash_days) if raw_trash_days is not None else None
        lock_path = rt.state.journal_db.with_name(f"{rt.state.journal_db.name}.lock")

        count = execute_delete(
            plan,
            manifest=manifest,
            data_dir=data_dir,
            purge=purge,
            trash_days=trash_days,
            reason=reason,
            lock_path=lock_path,
        )
        _log_data_deleted(
            rt,
            reason=reason,
            selectors=selectors,
            instruments=plan.instruments,
            files_count=count,
            total_bytes=plan.total_bytes,
            purge=purge,
        )
        action_str = "purged permanently" if purge else "moved to safety trash"
        typer.echo(f"Successfully deleted {count} file(s) ({action_str}).")


@data_app.command("compact")
@handle_cli_errors
def data_compact(
    ctx: typer.Context,
    date_val: str | None = typer.Option(None, "--date", help="Date to compact (YYYY-MM-DD)"),
) -> None:
    """Compact raw partition parts into day.parquet."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest_db = rt.root / "data" / "state" / "data_manifest.db"
        manifest = DataManifest(manifest_db)
        recorder = MarketRecorder(data_dir=rt.root / "data", manifest=manifest)

        dates_to_compact: list[str] = (
            [date_val] if date_val else sorted({f.date for f in manifest.get_files(state="part")})
        )
        total_compacted = 0
        for d in dates_to_compact:
            compacted = recorder.compact(d)
            total_compacted += len(compacted)
        typer.echo(f"Compacted {total_compacted} partition(s).")


@data_app.command("synth")
@handle_cli_errors
def data_synth(
    ctx: typer.Context,
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated instrument symbols"),
    days: int = typer.Option(1, "--days", help="Number of trading days to generate"),
    rows: int = typer.Option(1000, "--rows", help="Rows per day per symbol"),
    seed: int = typer.Option(1, "--seed", help="Random seed for deterministic generation"),
) -> None:
    """Generate deterministic synthetic market data ticks."""
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    with _open_rt(ctx, ensure=False) as rt:
        manifest_db = rt.root / "data" / "state" / "data_manifest.db"
        manifest = DataManifest(manifest_db)
        total_rows = synth_ticks(
            symbols=sym_list,
            days=days,
            rows_per_day=rows,
            seed=seed,
            data_dir=rt.root / "data",
            manifest=manifest,
        )
        typer.echo(
            f"Generated {total_rows} synthetic ticks for {len(sym_list)} "
            f"symbol(s) across {days} day(s)."
        )


@data_trash_app.command("list")
@handle_cli_errors
def trash_list_cmd(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List directories and files in safety trash."""
    with _open_rt(ctx, ensure=False) as rt:
        items = list_trash(rt.root / "data")
        if json_out:
            typer.echo(json.dumps([asdict(i) for i in items], indent=2))
            return
        headers = ["DIRECTORY", "DELETED AT", "FILES", "BYTES", "REASON"]
        rows = [
            [i.name, i.deleted_at, str(i.files_count), str(i.bytes_count), i.reason] for i in items
        ]
        typer.echo(format_table(headers, rows))


@data_trash_app.command("purge")
@handle_cli_errors
def trash_purge_cmd(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Purge all files in safety trash permanently."""
    if not yes:
        typer.confirm("Permanently purge all data in safety trash?", abort=True)
    with _open_rt(ctx, ensure=False) as rt:
        purged = purge_trash(rt.root / "data")
        typer.echo(f"Purged {purged} trash directory/directories.")


@groups_app.command("list")
@handle_cli_errors
def groups_list_cmd(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List configured instrument groups."""
    with _open_rt(ctx, ensure=False) as rt:
        groups_dir = rt.root / "config" / "groups"
        details = list_group_details(groups_dir)
        if json_out:
            typer.echo(json.dumps(details, indent=2))
            return
        headers = ["NAME", "DESCRIPTION", "MEMBERS"]
        rows = []
        for d in details.values():
            m_count = len(d["members"])
            preview = ", ".join(d["members"][:3])
            suffix = "..." if m_count > 3 else ""
            rows.append([d["name"], d["description"], f"{m_count}: {preview}{suffix}"])
        typer.echo(format_table(headers, rows))


@groups_app.command("show")
@handle_cli_errors
def groups_show_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Group name"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show details and members of an instrument group."""
    with _open_rt(ctx, ensure=False) as rt:
        groups_dir = rt.root / "config" / "groups"
        details = list_group_details(groups_dir)
        if name not in details:
            known = list(details.keys())
            raise ConfigError(
                what=f"Instrument group '{name}' not found",
                where=f"config/groups/{name}.toml",
                fix=f"Available groups: {', '.join(known)}",
            )
        info = details[name]
        if json_out:
            typer.echo(json.dumps(info, indent=2))
            return
        typer.echo(f"Group: {info['name']}")
        if info["description"]:
            typer.echo(f"Description: {info['description']}")
        typer.echo("Members:")
        for m in info["members"]:
            typer.echo(f"  - {m}")
