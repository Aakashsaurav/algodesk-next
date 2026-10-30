"""Market data inspection, retrieval, deletion, and trash CLI commands."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from itertools import groupby
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import typer

from algodesk.api import InstrumentId, SeriesKey
from algodesk.api.errors import ConfigError, DataError
from algodesk.api.kinds import Mode
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.data.catalog import (
    coverage,
    execute_delete,
    list_trash,
    plan_delete,
    purge_trash,
    resolve_selector,
)
from algodesk.data.coverage import CoverageIndex
from algodesk.data.groups import get_group, list_group_details
from algodesk.data.layout import dataset_id_for_key, parse_bar_file_path
from algodesk.data.manifest import (
    CoverageDayRecord,
    DataManifest,
    DatasetRecord,
    PartRecord,
)
from algodesk.data.quarantine import QuarantineManager
from algodesk.data.recorder import MarketRecorder
from algodesk.data.schema import read_bars_parquet
from algodesk.data.session_bars import SessionBarStore
from algodesk.data.store import BarStore, _compute_sha256
from algodesk.data.synth import synth_ticks
from algodesk.data.validator import BarValidator
from algodesk.ops.context import build_context
from algodesk.ops.runtime import Runtime

data_app = typer.Typer(help="Fetch, inspect, delete, and manage market data.")
data_trash_app = typer.Typer(help="Manage data safety trash.")
groups_app = typer.Typer(help="Inspect and list configured instrument groups.")
quarantine_app = typer.Typer(help="Inspect, release, and purge quarantined market data.")
session_bars_app = typer.Typer(help="Manage ephemeral live session bars.")

data_app.add_typer(data_trash_app, name="trash")
data_app.add_typer(quarantine_app, name="quarantine")
data_app.add_typer(session_bars_app, name="session-bars")


def _get_manifest(rt: Runtime) -> DataManifest:
    data_dir = rt.paths.data
    m_path = data_dir / "state" / "data_manifest.db"
    if not m_path.exists() and (data_dir / "manifest.db").exists():
        m_path = data_dir / "manifest.db"
    return DataManifest(m_path)


def _purge_expired_quarantine(rt: Runtime, manifest: DataManifest) -> None:
    try:
        qm = QuarantineManager(data_dir=rt.paths.data, manifest=manifest, params=rt.params)
        qm.purge_expired()
    except Exception:
        pass


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
        manifest = _get_manifest(rt)
        _purge_expired_quarantine(rt, manifest)
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
        manifest = _get_manifest(rt)
        _purge_expired_quarantine(rt, manifest)
        groups_dir = rt.root / "config" / "groups"
        data_dir = rt.paths.data

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
    pattern: str = typer.Option("random", "--pattern", help="Data pattern: random or trend"),
) -> None:
    """Generate deterministic synthetic market data ticks."""
    if pattern not in ("random", "trend"):
        raise ConfigError(
            what=f"Invalid pattern '{pattern}'",
            where="data synth",
            fix="Use '--pattern random' or '--pattern trend'",
        )
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
            pattern=pattern,
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


@data_app.command("verify")
@handle_cli_errors
def data_verify(
    ctx: typer.Context,
    only: str = typer.Option("all", "--only", help="Verify only 'ticks', 'bars', or 'all'"),
    adopt: bool = typer.Option(False, "--adopt", help="Adopt orphan bar files into manifest"),
    json_out: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Verify integrity of files, checksums, manifest, rules, and orphans."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        _purge_expired_quarantine(rt, manifest)
        data_dir = rt.paths.data

        check_ticks = only.lower() in ("all", "ticks")
        check_bars = only.lower() in ("all", "bars")

        files_checked = 0
        missing_files: list[str] = []
        checksum_errors: list[str] = []
        rule_violations: list[str] = []
        orphans: list[str] = []
        adopted: list[str] = []

        known_tick_paths: set[str] = set()
        known_bar_paths: set[str] = set()

        if check_ticks:
            for f in manifest.get_files():
                files_checked += 1
                known_tick_paths.add(f.path)
                try:
                    rel_f = str(Path(f.path).relative_to(data_dir))
                    known_tick_paths.add(rel_f)
                except ValueError:
                    pass
                f_path = data_dir / f.path if not Path(f.path).is_absolute() else Path(f.path)
                if not f_path.exists():
                    missing_files.append(f.path)
                    continue
                actual_sha = _compute_sha256(f_path)
                if actual_sha != f.sha256:
                    checksum_errors.append(
                        f"{f.path}: sha mismatch (expected {f.sha256[:8]}, got {actual_sha[:8]})"
                    )

        if check_bars:
            datasets = {d.dataset_id: d for d in manifest.list_datasets()}
            for p in manifest.get_parts():
                files_checked += 1
                known_bar_paths.add(p.path)
                try:
                    rel_p = str(Path(p.path).relative_to(data_dir))
                    known_bar_paths.add(rel_p)
                except ValueError:
                    pass
                p_path = data_dir / p.path if not Path(p.path).is_absolute() else Path(p.path)
                if not p_path.exists():
                    missing_files.append(p.path)
                    continue
                actual_sha = _compute_sha256(p_path)
                if actual_sha != p.sha256:
                    checksum_errors.append(
                        f"{p.path}: sha mismatch (expected {p.sha256[:8]}, got {actual_sha[:8]})"
                    )
                else:
                    ds = datasets.get(p.dataset_id)
                    if ds is not None:
                        try:
                            bars = read_bars_parquet(
                                p_path, InstrumentId(ds.instrument), ds.timeframe
                            )
                            for idx in range(len(bars) - 1):
                                if bars[idx].start_ts >= bars[idx + 1].start_ts:
                                    rule_violations.append(
                                        f"{p.path}: non-increasing timestamps at row {idx}"
                                    )
                                    break
                        except Exception as exc:
                            rule_violations.append(f"{p.path}: read error ({exc})")

        if check_ticks and (data_dir / "ticks").exists():
            for path_obj in (data_dir / "ticks").rglob("*.parquet"):
                try:
                    rel = str(path_obj.relative_to(data_dir))
                except ValueError:
                    rel = str(path_obj)
                if rel not in known_tick_paths:
                    orphans.append(rel)

        if check_bars and (data_dir / "bars").exists():
            for path_obj in (data_dir / "bars").rglob("*.parquet"):
                try:
                    rel = str(path_obj.relative_to(data_dir))
                except ValueError:
                    rel = str(path_obj)
                if rel not in known_bar_paths:
                    orphans.append(rel)

        if adopt and orphans:
            remaining_orphans: list[str] = []
            val = BarValidator(data_dir=data_dir, manifest=manifest)
            now_iso = datetime.now(UTC).isoformat()
            for o_rel in orphans:
                if not o_rel.startswith("bars/"):
                    remaining_orphans.append(o_rel)
                    continue
                o_path = data_dir / o_rel
                info = parse_bar_file_path(o_path)
                if not info:
                    remaining_orphans.append(o_rel)
                    continue

                inst_id = InstrumentId(f"{info['exchange']}:{info['segment']}:{info['symbol']}")
                key = SeriesKey(
                    instrument=inst_id,
                    kind="bars",
                    timeframe=info["timeframe"],
                    adjustment=info["adj"],
                    source=info["source"],
                )
                try:
                    bars = read_bars_parquet(o_path, inst_id, info["timeframe"])
                    rep = val.validate_chunk(bars, key)
                    if rep.is_valid:
                        ds_id = dataset_id_for_key(key)
                        manifest.record_dataset(
                            DatasetRecord(
                                dataset_id=ds_id,
                                instrument=str(inst_id),
                                kind=key.kind,
                                timeframe=key.timeframe,
                                adjustment=key.adjustment,
                                source=key.source,
                                schema_version=1,
                                created_at=now_iso,
                            )
                        )
                        sha = _compute_sha256(o_path)
                        f_bytes = o_path.stat().st_size
                        manifest.record_part(
                            PartRecord(
                                path=o_rel,
                                dataset_id=ds_id,
                                rows=len(bars),
                                bytes=f_bytes,
                                first_ts=bars[0].start_ts.isoformat(),
                                last_ts=bars[-1].start_ts.isoformat(),
                                sha256=sha,
                                source=key.source,
                                source_version="1.0",
                                fetched_at=now_iso,
                            )
                        )
                        cov_index = CoverageIndex(manifest=manifest)
                        for d_val, day_bars_iter in groupby(bars, key=lambda b: b.start_ts.date()):
                            day_bars = list(day_bars_iter)
                            st = cov_index.evaluate_day_status(ds_id, d_val, has_bars=True)
                            manifest.record_coverage_day(
                                CoverageDayRecord(
                                    dataset_id=ds_id,
                                    day=d_val.isoformat(),
                                    status=st,
                                    rows=len(day_bars),
                                    expected_rows=len(day_bars),
                                    part_path=o_rel,
                                    fetched_at=now_iso,
                                )
                            )
                        adopted.append(o_rel)
                    else:
                        remaining_orphans.append(o_rel)
                except Exception:
                    remaining_orphans.append(o_rel)
            orphans = remaining_orphans

        is_ok = not missing_files and not checksum_errors and not rule_violations and not orphans

        if json_out:
            res_dict = {
                "ok": is_ok,
                "files_checked": files_checked,
                "missing_files": missing_files,
                "checksum_errors": checksum_errors,
                "rule_violations": rule_violations,
                "orphans": orphans,
                "adopted": adopted,
            }
            typer.echo(json.dumps(res_dict, indent=2))
            if not is_ok:
                raise typer.Exit(code=1)
            return

        if not is_ok:
            typer.secho("VERIFY: FAILED", fg=typer.colors.RED, bold=True)
            if missing_files:
                preview = ", ".join(missing_files[:5])
                typer.echo(f"  Missing files ({len(missing_files)}): {preview}")
            if checksum_errors:
                preview = ", ".join(checksum_errors[:5])
                typer.echo(f"  Checksum mismatches ({len(checksum_errors)}): {preview}")
            if rule_violations:
                preview = ", ".join(rule_violations[:5])
                typer.echo(f"  Rule violations ({len(rule_violations)}): {preview}")
            if orphans:
                preview = ", ".join(orphans[:5])
                typer.echo(f"  Orphans ({len(orphans)}): {preview} (use --adopt to register)")
            raise typer.Exit(code=1)

        msg = f"VERIFY: OK (checked {files_checked} files, {len(orphans)} orphans, 0 errors)"
        if adopted:
            msg += f" [Adopted {len(adopted)} orphan(s)]"
        typer.secho(msg, fg=typer.colors.GREEN)


@data_app.command("status")
@handle_cli_errors
def data_status(
    ctx: typer.Context,
    group: bool = typer.Option(False, "--group", help="Group coverage by instrument group"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Display overall status and coverage of the market data store."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        _purge_expired_quarantine(rt, manifest)
        data_dir = rt.paths.data
        groups_dir = rt.root / "config" / "groups"

        if group:
            rows = coverage(manifest, group_by="group", groups_dir=groups_dir)
            if json_out:
                typer.echo(json.dumps([asdict(r) for r in rows], indent=2))
                return
            headers = [
                "GROUP",
                "KIND",
                "TIMEFRAME",
                "FIRST DATE",
                "LAST DATE",
                "DAYS",
                "ROWS",
                "BYTES",
                "SOURCE",
            ]
            t_rows = [
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
            typer.echo(format_table(headers, t_rows))
            return

        datasets = manifest.list_datasets()
        parts = manifest.get_parts()
        files = manifest.get_files()
        quarantined = manifest.get_quarantine(status="quarantined")
        trash_items = list_trash(data_dir)

        total_rows = sum(p.rows for p in parts) + sum(f.rows for f in files)
        total_bytes = sum(p.bytes for p in parts) + sum(f.bytes for f in files)

        if json_out:
            out = {
                "datasets": len(datasets),
                "bar_files": len(parts),
                "tick_files": len(files),
                "total_rows": total_rows,
                "total_bytes": total_bytes,
                "quarantined_chunks": len(quarantined),
                "trash_items": len(trash_items),
            }
            typer.echo(json.dumps(out, indent=2))
            return

        headers = ["METRIC", "VALUE"]
        metric_rows = [
            ["Datasets", str(len(datasets))],
            ["Bar Files (parts)", str(len(parts))],
            ["Tick Files", str(len(files))],
            ["Total Rows", f"{total_rows:,}"],
            ["Total Storage", f"{total_bytes / (1024**2):.2f} MB" if total_bytes > 0 else "0 B"],
            ["Quarantined Chunks", str(len(quarantined))],
            ["Trash Entries", str(len(trash_items))],
        ]
        typer.echo(format_table(headers, metric_rows))


@data_app.command("gaps")
@handle_cli_errors
def data_gaps(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Instrument selector or @group"),
    from_date: str | None = typer.Option(None, "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str | None = typer.Option(None, "--to", help="End date (YYYY-MM-DD)"),
    timeframe: str = typer.Option("1m", "--timeframe", "-t", help="Bar timeframe"),
    source: str = typer.Option("upstox", "--source", help="Data source"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Inspect and report missing trading day gaps across instruments."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        _purge_expired_quarantine(rt, manifest)
        groups_dir = rt.root / "config" / "groups"
        try:
            instruments = resolve_selector(selector, manifest, groups_dir=groups_dir)
        except DataError:
            from algodesk.api.market import parse_id

            try:
                parse_id(selector)
                instruments = [selector]
            except Exception:
                raise

        start_d = (
            date.fromisoformat(from_date) if from_date else (date.today() - timedelta(days=30))
        )
        end_d = date.fromisoformat(to_date) if to_date else date.today()

        cov_index = CoverageIndex(manifest=manifest)
        all_gaps: list[dict[str, str]] = []

        for inst in instruments:
            key = SeriesKey(
                instrument=InstrumentId(inst),
                kind="bars",
                timeframe=timeframe,
                adjustment="raw",
                source=source,
            )
            ds_id = dataset_id_for_key(key)
            gaps = cov_index.detect_gaps(ds_id, start_d, end_d)
            for g in gaps:
                all_gaps.append(
                    {
                        "instrument": inst,
                        "day": g.day.isoformat(),
                        "status": str(g.status),
                        "reason": g.reason,
                    }
                )

        if json_out:
            typer.echo(json.dumps(all_gaps, indent=2))
            return

        if not all_gaps:
            typer.echo(f"No gaps found for '{selector}' between {start_d} and {end_d}.")
            return

        headers = ["INSTRUMENT", "DAY", "STATUS", "REASON"]
        rows = [[g["instrument"], g["day"], g["status"], g["reason"]] for g in all_gaps]
        typer.echo(format_table(headers, rows))


@quarantine_app.command("list")
@handle_cli_errors
def quarantine_list(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List quarantined market data chunks."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        _purge_expired_quarantine(rt, manifest)
        items = manifest.get_quarantine()
        if json_out:
            typer.echo(json.dumps([asdict(i) for i in items], indent=2))
            return
        if not items:
            typer.echo("Quarantine is clean (0 items).")
            return
        headers = ["ID", "CHUNK", "STATUS", "REASON", "CREATED"]
        rows = [[i.id, i.chunk, i.status, i.reason[:40], i.created[:19]] for i in items]
        typer.echo(format_table(headers, rows))


@quarantine_app.command("show")
@handle_cli_errors
def quarantine_show(
    ctx: typer.Context,
    id: str = typer.Argument(..., help="Quarantine item ID"),
) -> None:
    """Show detailed report for a quarantined item."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        qm = QuarantineManager(data_dir=rt.paths.data, manifest=manifest, params=rt.params)
        info = qm.show(id)
        typer.echo(json.dumps(info, indent=2))


@quarantine_app.command("release")
@handle_cli_errors
def quarantine_release(
    ctx: typer.Context,
    id: str = typer.Argument(..., help="Quarantine item ID to release"),
    reason: str = typer.Option(..., "--reason", help="Mandatory explanation for releasing"),
) -> None:
    """Release and commit quarantined bars to the historical store."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        store = BarStore(data_dir=rt.paths.data, manifest=manifest, params=rt.params)
        qm = QuarantineManager(
            data_dir=rt.paths.data, manifest=manifest, store=store, params=rt.params
        )
        qm.release(id, reason=reason)
        typer.secho(
            f"Released quarantined chunk '{id}' and committed to store.",
            fg=typer.colors.GREEN,
        )


@quarantine_app.command("purge")
@handle_cli_errors
def quarantine_purge(
    ctx: typer.Context,
    id: str | None = typer.Option(None, "--id", help="Specific quarantine item ID to purge"),
    all_items: bool = typer.Option(False, "--all", help="Purge all quarantined items"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Permanently delete quarantined chunks."""
    if not id and not all_items:
        raise ConfigError(
            what="Must specify either --id <ID> or --all to purge quarantine",
            where="data quarantine purge",
            fix="Specify --id <id> or --all",
        )
    if not yes:
        confirm = typer.prompt("Type 'PURGE' to confirm deletion of quarantined data")
        if confirm.strip() != "PURGE":
            typer.echo("Aborted.")
            return

    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        qm = QuarantineManager(data_dir=rt.paths.data, manifest=manifest, params=rt.params)
        purged = qm.purge(id=id, all_items=all_items)
        typer.echo(f"Purged {purged} quarantined item(s).")


@session_bars_app.command("purge")
@handle_cli_errors
def session_bars_purge(
    ctx: typer.Context,
    days: int | None = typer.Option(None, "--days", help="Purge older than N days"),
) -> None:
    """Purge live session bar partitions older than keep_days retention."""
    with _open_rt(ctx, ensure=False) as rt:
        sbs = SessionBarStore(data_dir=rt.paths.data, params=rt.params)
        purged = sbs.purge_older_than(days=days)
        typer.echo(f"Purged {purged} session bar date partition(s).")
