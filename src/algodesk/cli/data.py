"""Market data inspection, retrieval, deletion, and trash CLI commands."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import typer

from algodesk.api import ActionRecord, Bar, InstrumentId, SeriesKey
from algodesk.api.errors import ConfigError, DataError
from algodesk.api.kinds import Mode
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.cli.secrets import _resolve_secrets_provider
from algodesk.data.actions_store import ActionsStore, CorporateActionsManager
from algodesk.data.catalog import (
    coverage,
    execute_delete,
    list_trash,
    plan_delete,
    purge_trash,
    resolve_selector,
)
from algodesk.data.coverage import CoverageIndex
from algodesk.data.detection import detect_price_anomalies
from algodesk.data.groups import get_group, list_group_details
from algodesk.data.layout import dataset_id_for_key, parse_bar_file_path
from algodesk.data.manifest import (
    CoverageDayRecord,
    DataManifest,
    DatasetRecord,
    FileRecord,
    PartRecord,
)
from algodesk.data.overrides import (
    ActionOverride,
    add_override,
)
from algodesk.data.quarantine import QuarantineManager
from algodesk.data.reconciler import CorporateActionsReconciler
from algodesk.data.recorder import MarketRecorder
from algodesk.data.schema import read_bars_parquet, write_bars_parquet
from algodesk.data.service import DataService
from algodesk.data.session_bars import SessionBarStore
from algodesk.data.store import BarStore, _compute_sha256
from algodesk.data.synth import synth_ticks
from algodesk.data.validator import BarValidator
from algodesk.logs.formatting import format_ist
from algodesk.ops.context import build_context
from algodesk.ops.runtime import Runtime

data_app = typer.Typer(help="Fetch, inspect, delete, and manage market data.")


@data_app.callback()
def data_callback(
    refresh_master: bool = typer.Option(
        False, "--refresh-master", help="Force fresh download of instrument master"
    ),
) -> None:
    """Market data inspection, retrieval, deletion, and trash CLI commands."""
    import os

    if refresh_master:
        os.environ["UPSTOX_REFRESH_MASTER"] = "1"


actions_app = typer.Typer(help="Manage, refresh, inspect, and reconcile corporate actions.")
data_trash_app = typer.Typer(help="Manage data safety trash.")
groups_app = typer.Typer(help="Inspect and list configured instrument groups.")
quarantine_app = typer.Typer(help="Inspect, release, and purge quarantined market data.")
session_bars_app = typer.Typer(help="Manage ephemeral live session bars.")
override_app = typer.Typer(help="Manage corporate actions operator overrides.")

data_app.add_typer(actions_app, name="actions")
actions_app.add_typer(override_app, name="override")
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


def _coverage_row(r: Any) -> list[str]:
    a = [r.target, r.kind, r.timeframe, r.first_date, r.last_date]
    b = [str(r.days), str(r.rows), str(r.bytes), r.source]
    return a + b


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


def _build_data_service(rt: Runtime, source: str = "yfinance") -> DataService:
    sec_prov = _resolve_secrets_provider(rt)
    p_ctx = build_context(Mode.RESEARCH, run_id=f"data_ds_{source}", secrets=sec_prov)
    broker: Any = rt.plugins.create("broker", source, config={}, ctx=p_ctx)
    manifests = {p.manifest.name: p.manifest for p in rt.plugins.list() if hasattr(p, "manifest")}
    store = BarStore(
        getattr(getattr(rt, "paths", None), "data", Path("data")),
        _get_manifest(rt),
        params=rt.params,
        journal=getattr(rt, "journal", None),
    )
    j = getattr(rt, "journal", None)
    return DataService(
        {source: broker}, source, store, None, manifests=manifests, params=rt.params, journal=j
    )


@data_app.command("get")
@handle_cli_errors
def data_get(
    ctx: typer.Context,
    symbol: str = typer.Argument(..., help="Symbol (e.g. RELIANCE, INFY)"),
    timeframe: str = typer.Argument(..., help="Timeframe (e.g. 1m, 1d)"),
    start: str = typer.Argument(..., help="Start date/time"),
    end: str = typer.Argument(..., help="End date/time"),
    adjusted: bool = typer.Option(False, "--adjusted", help="Use adjusted prices"),
    download: str = typer.Option("adjusted", "--download", help="Download raw or adjusted series"),
    adjust: str | None = typer.Option(None, "--adjust", help="Adjustment: none, splits, total"),
    accept_unverified: bool = typer.Option(False, "--accept-unverified", help="Accept unverified"),
    fill_only: bool = typer.Option(False, "--fill-only", help="Fetch and commit without table"),
    source: str = typer.Option("yfinance", "--source", help="Data source broker plugin"),
) -> None:
    """Fetch and display historical market data bars in a table."""
    start_dt, end_dt = _parse_data_cli_dt(start, is_end=False), _parse_data_cli_dt(end, is_end=True)
    download_mode = "adjusted" if adjusted else download
    inst_id = InstrumentId(symbol if ":" in symbol else f"NSE:EQ:{symbol}")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe=timeframe,
        adjustment=download_mode,
        source=source,
    )

    with _open_rt(ctx, ensure=False) as rt:
        ds = _build_data_service(rt, source)
        data_slice = ds.get(
            series=key,
            start=start_dt,
            end=end_dt,
            download=download_mode,
            adjust=adjust,
            fill_only=fill_only,
            accept_unverified=accept_unverified,
        )

    if fill_only:
        typer.echo(f"Filled data for {symbol} ({timeframe}) between {start} and {end}.")
        return

    bars = list(data_slice.bars)
    if not bars:
        typer.echo(f"No bars returned for {symbol} ({timeframe}) between {start} and {end}.")
        if data_slice.gaps:
            empty_cnt = sum(1 for g in data_slice.gaps if str(g.status) == "EMPTY")
            typer.echo(
                f"Gap summary: {len(data_slice.gaps)} gap session(s) detected ({empty_cnt} EMPTY)."
            )
        return

    is_daily = timeframe.lower() == "1d"
    headers = (
        ["DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
        if is_daily
        else ["DATE/TIME (IST)", "OPEN", "HIGH", "LOW", "CLOSE", "VOLUME"]
    )
    rows = [
        [
            format_ist(b.start_ts, date_only=is_daily),
            f"{b.open:.2f}",
            f"{b.high:.2f}",
            f"{b.low:.2f}",
            f"{b.close:.2f}",
            str(b.volume),
        ]
        for b in bars
    ]
    typer.echo(format_table(headers, rows))
    if data_slice.provenance and (warns := data_slice.provenance.get("warnings")):
        typer.echo("\nWarnings:")
        for w in warns:
            typer.echo(f"  - {w}")
    if data_slice.gaps:
        empty_cnt = sum(1 for g in data_slice.gaps if str(g.status) == "EMPTY")
        typer.echo(
            f"\nGap summary: {len(data_slice.gaps)} gap session(s) detected ({empty_cnt} EMPTY)."
        )


def _journal_data_event(rt: Runtime, event_type: str, payload: dict[str, Any], caller: str) -> None:
    if (j := getattr(rt, "journal", None)) is not None:
        j.append(event_type, payload, durable=True)
        return
    try:
        from algodesk.journal.writer import Journal, JournalSettings

        p = rt.params
        s = JournalSettings(
            flush_interval_ms=int(p.get("journal.flush_interval_ms")) if p else 50,
            max_buffer_events=int(p.get("journal.max_buffer_events")) if p else 1000,
            backpressure_timeout_ms=int(p.get("journal.backpressure_timeout_ms")) if p else 5000,
        )
        with Journal(rt.state.journal_db, settings=s) as jnl:
            jnl.start_run("research", "core", "maint", caller)
            jnl.append(event_type, payload, durable=True)
            jnl.end_run("ok")
    except Exception:
        pass


def _log_data_deleted(
    rt: Runtime,
    reason: str,
    selectors: Sequence[str],
    instruments: Sequence[str],
    files_count: int,
    total_bytes: int,
    purge: bool,
) -> None:
    payload = {
        "reason": reason,
        "selectors": list(selectors),
        "instruments": list(instruments),
        "files_count": files_count,
        "bytes": total_bytes,
        "purge": purge,
    }
    _journal_data_event(rt, "data.deleted", payload, "data_delete")


@data_app.command("list")
@handle_cli_errors
def data_list(
    ctx: typer.Context,
    group_by: str = typer.Option("none", "--group-by", help="Grouping: exchange|segment|group"),
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

        t_col = group_by.upper() if group_by != "none" else "INSTRUMENT"
        headers = [t_col] + "KIND|TIMEFRAME|FIRST DATE|LAST DATE|DAYS|ROWS|BYTES|SOURCE".split("|")
        typer.echo(format_table(headers, [_coverage_row(r) for r in rows]))


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
        manifest = DataManifest(rt.root / "data" / "state" / "data_manifest.db")
        recorder = MarketRecorder(data_dir=rt.root / "data", manifest=manifest)
        dates = (
            [date_val] if date_val else sorted({f.date for f in manifest.get_files(state="part")})
        )
        total = sum(len(recorder.compact(d)) for d in dates)
        typer.echo(f"Compacted {total} partition(s).")


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
        manifest = DataManifest(rt.root / "data" / "state" / "data_manifest.db")
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

        def _p(m: list[str]) -> str:
            return f"{len(m)}: {', '.join(m[:3])}{'...' if len(m) > 3 else ''}"

        headers = ["NAME", "DESCRIPTION", "MEMBERS"]
        rows = [[d["name"], d["description"], _p(d["members"])] for d in details.values()]
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
        desc = f"\nDescription: {info['description']}" if info["description"] else ""
        m_lines = "\n".join(f"  - {m}" for m in info["members"])
        typer.echo(f"Group: {info['name']}{desc}\nMembers:\n{m_lines}")


def _check_manifest_file(
    path_str: str,
    expected_sha: str,
    data_dir: Path,
    missing: list[str],
    errors: list[str],
) -> Path | None:
    p = data_dir / path_str if not Path(path_str).is_absolute() else Path(path_str)
    if not p.exists():
        missing.append(path_str)
        return None
    if (sha := _compute_sha256(p)) != expected_sha:
        errors.append(f"{path_str}: sha mismatch ({expected_sha[:8]} != {sha[:8]})")
        return None
    return p


def _find_dir_orphans(target: Path, data_dir: Path, known: set[str], orphans: list[str]) -> None:
    if not target.exists():
        return
    for p in target.rglob("*.parquet"):
        try:
            rel = str(p.relative_to(data_dir))
        except ValueError:
            rel = str(p)
        if rel not in known:
            orphans.append(rel)


def _adopt_bar(
    manifest: Any, key: Any, o_rel: str, o_path: Path, bars: list[Any], now_iso: str
) -> None:
    ds_id = dataset_id_for_key(key)
    if key.source == "upstox":
        basis = "splits"
    elif key.source == "yfinance":
        basis = "total" if key.adjustment == "adjusted" else "splits"
    else:
        basis = "splits" if key.adjustment == "raw" else "total"

    manifest.record_dataset(
        DatasetRecord(
            dataset_id=ds_id,
            instrument=str(key.instrument),
            kind=key.kind,
            timeframe=key.timeframe,
            adjustment=key.adjustment,
            source=key.source,
            schema_version=1,
            created_at=now_iso,
            actions_epoch=None,
            adjustment_basis=basis,
        )
    )
    sha, f_bytes = _compute_sha256(o_path), o_path.stat().st_size
    manifest.record_part(
        PartRecord(
            o_rel,
            ds_id,
            len(bars),
            f_bytes,
            bars[0].start_ts.isoformat(),
            bars[-1].start_ts.isoformat(),
            sha,
            key.source,
            "1.0",
            now_iso,
        )
    )
    cov_index = CoverageIndex(manifest=manifest)
    for d_val, day_bars_iter in groupby(bars, key=lambda b: b.start_ts.date()):
        day_bars = list(day_bars_iter)
        st = cov_index.evaluate_day_status(ds_id, d_val, has_bars=True)
        manifest.record_coverage_day(
            CoverageDayRecord(
                ds_id, d_val.isoformat(), st, len(day_bars), len(day_bars), o_rel, now_iso
            )
        )


@data_app.command("verify")
@handle_cli_errors
def data_verify(
    ctx: typer.Context,
    only: str = typer.Option("all", "--only", help="Verify 'ticks', 'bars', 'actions', 'all'"),
    adopt: bool = typer.Option(False, "--adopt", help="Adopt orphan bar files into manifest"),
    fix: bool = typer.Option(False, "--fix", help="Move offending bars to quarantine"),
    json_out: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Verify integrity of files, checksums, manifest, rules, and orphans."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        _purge_expired_quarantine(rt, manifest)
        data_dir = rt.paths.data

        check_ticks = only.lower() in ("all", "ticks")
        check_bars = only.lower() in ("all", "bars")
        check_actions = only.lower() in ("all", "actions")

        files_checked = 0
        missing_files: list[str] = []
        checksum_errors: list[str] = []
        rule_violations: list[str] = []
        orphans: list[str] = []
        adopted: list[str] = []
        quarantined_bars_count = 0

        known_tick_paths: set[str] = set()
        known_bar_paths: set[str] = set()
        known_action_paths: set[str] = set()

        if check_ticks:
            for f in manifest.get_files():
                if f.kind == "actions":
                    continue
                files_checked += 1
                known_tick_paths.add(f.path)
                try:
                    known_tick_paths.add(str(Path(f.path).relative_to(data_dir)))
                except ValueError:
                    pass
                _check_manifest_file(f.path, f.sha256, data_dir, missing_files, checksum_errors)

        if check_actions:
            import pyarrow.parquet as pq

            for f in manifest.get_files(kind="actions"):
                files_checked += 1
                known_action_paths.add(f.path)
                try:
                    known_action_paths.add(str(Path(f.path).relative_to(data_dir)))
                except ValueError:
                    pass
                p = _check_manifest_file(f.path, f.sha256, data_dir, missing_files, checksum_errors)
                if p is not None:
                    try:
                        pq.read_table(p)
                    except Exception as exc:
                        rule_violations.append(f"{f.path}: read error ({exc})")
            _find_dir_orphans(data_dir / "actions", data_dir, known_action_paths, orphans)

        if check_bars:
            val = BarValidator(
                data_dir=data_dir,
                manifest=manifest,
                params=getattr(rt, "params", None),
                journal=getattr(rt, "journal", None),
            )
            datasets = {d.dataset_id: d for d in manifest.list_datasets()}
            now_iso = datetime.now(UTC).isoformat()
            quarantined_bars_count = 0

            for part in manifest.get_parts():
                files_checked += 1
                known_bar_paths.add(part.path)
                try:
                    known_bar_paths.add(str(Path(part.path).relative_to(data_dir)))
                except ValueError:
                    pass
                p = _check_manifest_file(
                    part.path, part.sha256, data_dir, missing_files, checksum_errors
                )
                if p is not None:
                    ds = datasets.get(part.dataset_id)
                    if ds is not None:
                        try:
                            bars = read_bars_parquet(p, InstrumentId(ds.instrument), ds.timeframe)
                            key = SeriesKey(
                                instrument=InstrumentId(ds.instrument),
                                kind="bars",
                                timeframe=ds.timeframe,
                                adjustment=ds.adjustment,
                                source=ds.source,
                            )
                            part_violations: list[str] = []
                            offending_indices: set[int] = set()
                            seen_ts: set[datetime] = set()
                            prev_b: Bar | None = None

                            for idx, b in enumerate(bars):
                                is_flat = val._is_flat_zero_bar(b)
                                if is_flat:
                                    offending_indices.add(idx)
                                    part_violations.append(
                                        f"{part.path}: flat zero-volume bar on {b.start_ts.date()}"
                                    )
                                if prev_b is not None and b.start_ts <= prev_b.start_ts:
                                    offending_indices.add(idx)
                                    part_violations.append(
                                        f"{part.path}: non-increasing timestamps at row {idx}"
                                    )
                                if b.start_ts in seen_ts:
                                    offending_indices.add(idx)
                                    part_violations.append(
                                        f"{part.path}: duplicate timestamp at row {idx}"
                                    )
                                seen_ts.add(b.start_ts)
                                if not is_flat:
                                    prev_b = b

                            if offending_indices and fix:
                                offending_bars = [bars[i] for i in sorted(offending_indices)]
                                clean_bars = [
                                    bars[i] for i in range(len(bars)) if i not in offending_indices
                                ]
                                q_id = val._quarantine_chunk(
                                    offending_bars,
                                    key,
                                    errors=part_violations
                                    or ["Offending bars quarantined by verify --fix"],
                                    warnings=[],
                                )
                                quarantined_bars_count += len(offending_bars)
                                _journal_data_event(
                                    rt,
                                    "data.quarantine",
                                    {
                                        "id": q_id,
                                        "action": "quarantined",
                                        "instrument": str(key.instrument),
                                        "reason": part_violations[0]
                                        if part_violations
                                        else "Offending bars quarantined",
                                        "errors_count": len(offending_bars),
                                    },
                                    caller="data_verify",
                                )

                                if clean_bars:
                                    write_bars_parquet(p, clean_bars)
                                    new_sha = _compute_sha256(p)
                                    new_size = p.stat().st_size
                                    manifest.record_part(
                                        PartRecord(
                                            part.path,
                                            part.dataset_id,
                                            len(clean_bars),
                                            new_size,
                                            clean_bars[0].start_ts.isoformat(),
                                            clean_bars[-1].start_ts.isoformat(),
                                            new_sha,
                                            part.source,
                                            part.source_version,
                                            now_iso,
                                        )
                                    )
                                else:
                                    p.unlink(missing_ok=True)
                                    known_bar_paths.discard(part.path)
                                    try:
                                        known_bar_paths.discard(
                                            str(Path(part.path).relative_to(data_dir))
                                        )
                                    except ValueError:
                                        pass
                                    manifest.delete_part(part.path)

                                offending_days = {b.start_ts.date() for b in offending_bars}
                                cov_index = CoverageIndex(manifest=manifest)
                                for o_day in offending_days:
                                    day_clean_bars = [
                                        b for b in clean_bars if b.start_ts.date() == o_day
                                    ]
                                    st = cov_index.evaluate_day_status(
                                        ds.dataset_id, o_day, has_bars=bool(day_clean_bars)
                                    )
                                    manifest.record_coverage_day(
                                        CoverageDayRecord(
                                            ds.dataset_id,
                                            o_day.isoformat(),
                                            st,
                                            len(day_clean_bars),
                                            len(day_clean_bars),
                                            part.path if day_clean_bars else "",
                                            now_iso,
                                            note="Quarantined offending bars"
                                            if not day_clean_bars
                                            else "",
                                        )
                                    )
                            else:
                                rule_violations.extend(part_violations)
                        except Exception as exc:
                            rule_violations.append(f"{part.path}: read error ({exc})")
            _find_dir_orphans(data_dir / "bars", data_dir, known_bar_paths, orphans)

        if check_ticks:
            _find_dir_orphans(data_dir / "ticks", data_dir, known_tick_paths, orphans)

        if adopt and orphans:
            remaining_orphans: list[str] = []
            val = BarValidator(data_dir=data_dir, manifest=manifest)
            now_iso = datetime.now(UTC).isoformat()
            for o_rel in orphans:
                if o_rel.startswith("actions/"):
                    o_path = data_dir / o_rel
                    try:
                        import pyarrow.parquet as pq

                        tbl = pq.read_table(o_path)
                        sha = _compute_sha256(o_path)
                        parts = o_rel.split("/")
                        exch = "NSE"
                        sym = "UNKNOWN"
                        for part_seg in parts:
                            if part_seg.startswith("exchange="):
                                exch = part_seg.split("=")[1].upper()
                            elif part_seg.startswith("symbol="):
                                sym = part_seg.split("=")[1].upper()
                        rec = FileRecord(
                            o_rel,
                            f"{exch}:EQ:{sym}",
                            "actions",
                            "actions",
                            datetime.now(UTC).date().isoformat(),
                            len(tbl),
                            o_path.stat().st_size,
                            "",
                            "",
                            sha,
                            "adopted",
                            "ready",
                            now_iso,
                        )
                        manifest.record_file(rec)
                        adopted.append(o_rel)
                    except Exception:
                        remaining_orphans.append(o_rel)
                    continue

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
                    if val.validate_chunk(bars, key).is_valid:
                        _adopt_bar(manifest, key, o_rel, o_path, bars, now_iso)
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
                "quarantined_bars": quarantined_bars_count,
            }
            typer.echo(json.dumps(res_dict, indent=2))
            if not is_ok:
                raise typer.Exit(code=1)
            return

        if not is_ok:
            typer.secho("VERIFY: FAILED", fg=typer.colors.RED, bold=True)
            for lbl, items in [
                ("Missing files", missing_files),
                ("Checksum mismatches", checksum_errors),
                ("Rule violations", rule_violations),
                ("Orphans", orphans),
            ]:
                if items:
                    sfx = " (use --adopt to register)" if lbl == "Orphans" else ""
                    typer.echo(f"  {lbl} ({len(items)}): {', '.join(items[:5])}{sfx}")
            raise typer.Exit(code=1)

        msg = f"VERIFY: OK (checked {files_checked} files, {len(orphans)} orphans, 0 errors)"
        if adopted:
            msg += f" [Adopted {len(adopted)} orphan(s)]"
        if quarantined_bars_count > 0:
            msg += f" [Quarantined {quarantined_bars_count} offending bar(s)]"
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
                "GROUP"
            ] + "KIND|TIMEFRAME|FIRST DATE|LAST DATE|DAYS|ROWS|BYTES|SOURCE".split("|")
            typer.echo(format_table(headers, [_coverage_row(r) for r in rows]))
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
        size_str = f"{total_bytes / (1024**2):.2f} MB" if total_bytes > 0 else "0 B"
        pairs = [
            ("Datasets", len(datasets)),
            ("Bar Files (parts)", len(parts)),
            ("Tick Files", len(files)),
            ("Total Rows", f"{total_rows:,}"),
            ("Total Storage", size_str),
            ("Quarantined Chunks", len(quarantined)),
            ("Trash Entries", len(trash_items)),
        ]
        typer.echo(format_table(headers, [[m, str(v)] for m, v in pairs]))


@data_app.command("gaps")
@handle_cli_errors
def data_gaps(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Instrument selector or @group"),
    from_date: str | None = typer.Option(None, "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str | None = typer.Option(None, "--to", help="End date (YYYY-MM-DD)"),
    timeframe: str = typer.Option("1m", "--timeframe", "-t", help="Bar timeframe"),
    source: str | None = typer.Option(None, "--source", help="Data source"),
    adj: str | None = typer.Option(None, "--adj", help="Adjustment: raw or adjusted"),
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

            clean_sel = selector if ":" in selector else f"NSE:EQ:{selector}"
            try:
                parse_id(clean_sel)
                instruments = [clean_sel]
            except Exception:
                raise

        start_d = (
            date.fromisoformat(from_date) if from_date else (date.today() - timedelta(days=30))
        )
        end_d = date.fromisoformat(to_date) if to_date else date.today()

        cov_index = CoverageIndex(manifest=manifest)
        all_gaps: list[dict[str, str]] = []
        datasets_list = manifest.list_datasets(kind="bars")

        for inst in instruments:
            i_id = InstrumentId(inst)
            candidates = [
                d
                for d in datasets_list
                if d.instrument == inst
                and d.timeframe == timeframe
                and (source is None or d.source == source)
                and (adj is None or d.adjustment == adj)
            ]
            if candidates:
                candidates.sort(
                    key=lambda d: (
                        len(manifest.get_coverage_days(d.dataset_id)),
                        len(manifest.get_parts(d.dataset_id)),
                    ),
                    reverse=True,
                )
                selected_ds_id = candidates[0].dataset_id
            else:
                sel_src = source or "upstox"
                sel_adj = adj or "raw"
                selected_ds_id = dataset_id_for_key(
                    SeriesKey(i_id, "bars", timeframe, sel_adj, sel_src)
                )

            if not json_out:
                typer.echo(f"Checked series: {selected_ds_id}")

            for g in cov_index.detect_gaps(selected_ds_id, start_d, end_d):
                all_gaps.append(
                    {
                        "instrument": inst,
                        "series": selected_ds_id,
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

        headers = ["INSTRUMENT", "DAY (IST)", "STATUS", "REASON"]
        rows = [
            [g["instrument"], format_ist(g["day"], date_only=True), g["status"], g["reason"]]
            for g in all_gaps
        ]
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
        store = BarStore(rt.paths.data, manifest, params=rt.params)
        QuarantineManager(rt.paths.data, manifest, store=store, params=rt.params).release(
            id, reason=reason
        )
        msg = f"Released quarantined chunk '{id}' and committed to store."
        typer.secho(msg, fg=typer.colors.GREEN)


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
        if typer.prompt("Type 'PURGE' to confirm deletion of quarantined data").strip() != "PURGE":
            typer.echo("Aborted.")
            return

    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        purged = QuarantineManager(rt.paths.data, manifest, params=rt.params).purge(
            id=id, all_items=all_items
        )
        typer.echo(f"Purged {purged} quarantined item(s).")


@session_bars_app.command("purge")
@handle_cli_errors
def session_bars_purge(
    ctx: typer.Context,
    days: int | None = typer.Option(None, "--days", help="Purge older than N days"),
) -> None:
    """Purge live session bar partitions older than keep_days retention."""
    with _open_rt(ctx, ensure=False) as rt:
        purged = SessionBarStore(rt.paths.data, params=rt.params).purge_older_than(days=days)
        typer.echo(f"Purged {purged} session bar date partition(s).")


def _build_action_sources(rt: Runtime, requested_source: str | None) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    sec_prov = _resolve_secrets_provider(rt)
    p_ctx = build_context(Mode.RESEARCH, run_id="data_actions", secrets=sec_prov)
    names = [requested_source] if requested_source else ["upstox", "yfinance"]
    for name in names:
        try:
            sources[name] = rt.plugins.create("broker", name, config={}, ctx=p_ctx)
        except Exception:
            if requested_source:
                raise
    return sources


@actions_app.command("refresh")
@handle_cli_errors
def actions_refresh(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Symbol, ID, or @group selector"),
    source: str | None = typer.Option(None, "--source", help="Data source broker plugin"),
    force: bool = typer.Option(False, "--force", help="Force refresh even if within TTL"),
) -> None:
    """Refresh corporate actions for selector symbols older than data.actions_ttl_hours."""
    with _open_rt(ctx, ensure=False) as rt:
        mgr = CorporateActionsManager(
            ActionsStore(rt.paths.data, _get_manifest(rt)),
            _build_action_sources(rt, source),
            params=rt.params,
            journal=getattr(rt, "journal", None),
            groups_dir=Path("config/groups"),
        )
        res = mgr.refresh(selector, source=source, force=force)
        src_counts = res.get("source_counts", {})
        counts_str = (
            ", ".join(f"{k}: {v}" for k, v in sorted(src_counts.items())) if src_counts else ""
        )
        detail = f" ({counts_str})" if counts_str else ""
        typer.echo(
            f"Actions refresh complete: {len(res['refreshed'])} refreshed, "
            f"{len(res['skipped'])} cached, {res['actions_count']} events{detail}."
        )
        for warn in res.get("warnings", []):
            typer.echo(f"Warning: {warn}")


def _get_project_root(ctx: typer.Context) -> Path:
    from algodesk.cli.common import _get_cli_state
    from algodesk.ops.paths import find_project_root

    return Path(_get_cli_state(ctx).root or find_project_root()).resolve()


def _resolve_action_symbols(
    rt: Runtime, manifest: DataManifest, selector: str | None, exchange: str
) -> list[str]:
    if selector:
        try:
            res = resolve_selector(selector, manifest, groups_dir=Path("config/groups"))
            syms = [str(it).split(":")[-1].upper() for it in res]
            return list(dict.fromkeys(syms)) or [selector.upper()]
        except Exception:
            return [selector.upper()]
    p = rt.paths.data / "actions" / f"exchange={exchange.upper()}"
    if not p.exists():
        return []
    items = {f.parent.name.split("=")[-1].upper() for f in p.glob("symbol=*/actions.parquet")}
    return sorted(items)


def _build_reconciler(ctx: typer.Context, p: Any) -> CorporateActionsReconciler:
    return CorporateActionsReconciler(
        primary_source=str(p.get("corp.primary_source", "upstox")) if p else "upstox",
        dividend_tolerance_pct=float(p.get("corp.dividend_tolerance_pct", "1.0")) if p else 1.0,
        overrides_path=_get_project_root(ctx) / "config" / "corporate_actions_overrides.toml",
    )


ACTION_COLUMNS = ["Symbol", "Date", "Type", "Ratio or Amount", "Sources", "Status", "Reason"]


def _format_action_row(
    symbol: str,
    dt_str: str,
    ev_type: str,
    ratio: float | None,
    amount: Any,
    sources_str: str,
    status: str,
    reason: str,
) -> list[str]:
    if ratio is not None:
        r_or_a = f"{ratio:g}" if isinstance(ratio, float) and ratio.is_integer() else f"{ratio}"
    elif amount is not None:
        r_or_a = str(amount)
    else:
        r_or_a = "-"
    return [
        symbol,
        dt_str,
        ev_type,
        r_or_a,
        sources_str or "-",
        status,
        reason or "-",
    ]


def _format_summary_counts(statuses: Sequence[str]) -> str:
    from collections import Counter

    counts = Counter(statuses)
    return ", ".join(f"{st}={cnt}" for st, cnt in sorted(counts.items()))


@actions_app.command("list")
@handle_cli_errors
def actions_list(
    ctx: typer.Context,
    selector: str | None = typer.Argument(None, help="Optional symbol, ID, or @group selector"),
    raw: bool = typer.Option(False, "--raw", help="Show raw un-reconciled actions"),
    exchange: str = typer.Option("NSE", "--exchange", help="Exchange code"),
) -> None:
    """List recorded or reconciled corporate actions."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        store = ActionsStore(data_dir=rt.paths.data, manifest=manifest)
        symbols = _resolve_action_symbols(rt, manifest, selector, exchange)
        if not symbols:
            typer.echo("No corporate actions found.")
            return

        reconciler = _build_reconciler(ctx, rt.params)

        def _get_raw(s: str) -> Sequence[ActionRecord]:
            return store.read(exchange, s)

        def _get_reconciled(s: str) -> Sequence[ActionRecord]:
            return reconciler.reconcile(exchange, s, store.read(exchange, s))

        fetch_fn = _get_raw if raw else _get_reconciled
        rows = [
            _format_action_row(
                a.symbol,
                a.ex_date.isoformat(),
                a.type,
                a.ratio,
                a.amount,
                a.source,
                a.status,
                a.reason or a.text,
            )
            for s in symbols
            for a in fetch_fn(s)
        ]
        if not rows:
            typer.echo("No corporate actions found.")
            return

        typer.echo(format_table(ACTION_COLUMNS, rows))
        typer.echo(f"\nSummary counts: {_format_summary_counts([r[5] for r in rows])}")


@actions_app.command("check")
@handle_cli_errors
def actions_check(
    ctx: typer.Context,
    selector: str | None = typer.Argument(None, help="Optional symbol, ID, or @group selector"),
    raw: bool = typer.Option(False, "--raw", help="Check raw un-reconciled actions"),
    exchange: str = typer.Option("NSE", "--exchange", help="Exchange code"),
) -> None:
    """Check corporate actions for conflicts, unverified records, and price anomalies."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        store = ActionsStore(data_dir=rt.paths.data, manifest=manifest)
        symbols = _resolve_action_symbols(rt, manifest, selector, exchange)
        if not symbols:
            typer.echo("No corporate actions found to check.")
            return

        p = rt.params
        reconciler = _build_reconciler(ctx, p)
        jump_pct = float(p.get("corp.detect_jump_pct", "20.0")) if p else 20.0
        issue_rows: list[list[str]] = []
        for sym in symbols:
            raw_acts = store.read(exchange, sym)
            acts = raw_acts if raw else reconciler.reconcile(exchange, sym, raw_acts)
            for a in acts:
                if a.status in ("conflict", "manual_required", "unverified"):
                    issue_rows.append(
                        _format_action_row(
                            sym,
                            a.ex_date.isoformat(),
                            a.type,
                            a.ratio,
                            a.amount,
                            a.source,
                            a.status,
                            a.reason or a.text or f"Status {a.status}",
                        )
                    )

            exch_u = exchange.upper()
            day_p = rt.paths.data / "bars/1d" / f"exchange={exch_u}/symbol={sym}/day.parquet"
            if day_p.exists():
                bars = read_bars_parquet(day_p, InstrumentId(f"{exch_u}:EQ:{sym}"), "1d")
                for it in detect_price_anomalies(sym, bars, acts, detect_jump_pct=jump_pct):
                    issue_rows.append(
                        _format_action_row(
                            sym,
                            it.date.isoformat(),
                            "anomaly",
                            None,
                            None,
                            "detection",
                            it.kind,
                            it.details,
                        )
                    )

        if not issue_rows:
            typer.echo(f"Checked {len(symbols)} symbol(s): all corporate actions verified clean.")
            return

        typer.echo(format_table(ACTION_COLUMNS, issue_rows))
        typer.echo(f"\nSummary counts: {_format_summary_counts([r[5] for r in issue_rows])}")
        if any(r[5] in ("conflict", "manual_required", "suspect_event") for r in issue_rows):
            typer.echo(
                "\nAction required: resolve issues with 'algodesk data actions override add ...'"
            )


def _audit_symbol(
    ds: DataService,
    sym: str,
    exchange: str,
    start_dt: datetime,
    end_dt: datetime,
    from_date: str,
    to_date: str,
    tol: float,
) -> tuple[list[str], bool, list[str]]:
    import statistics

    inst = f"{exchange.upper()}:EQ:{sym.upper()}"
    try:
        kw = dict(download="raw", adjust="total", source="yfinance", accept_unverified=True)
        d_slice = ds.get(inst, start_dt, end_dt, **kw)  # type: ignore[arg-type]
        kw["download"], kw["adjust"] = "adjusted", "total"
        s_slice = ds.get(inst, start_dt, end_dt, **kw)  # type: ignore[arg-type]
    except Exception as exc:
        return [sym, "0", "N/A", "N/A", f"ERROR: {exc}"], True, []

    missing_cmds: list[str] = []
    base_cmd = f"algodesk data get {sym} 1d {from_date} {to_date} --source yfinance"
    if not d_slice.bars:
        missing_cmds.append(f"{base_cmd} --download raw")
    if not s_slice.bars:
        missing_cmds.append(f"{base_cmd} --download adjusted")
    if missing_cmds:
        return [sym, "0", "0.00%", "0.00%", "NO DATA"], False, missing_cmds

    d_map = {b.start_ts.date(): b.close for b in d_slice.bars}
    s_map = {b.start_ts.date(): b.close for b in s_slice.bars}
    common = sorted(set(d_map.keys()) & set(s_map.keys()))
    if not common:
        return [sym, "0", "0.00%", "0.00%", "NO DATA"], False, []

    diffs = [
        abs(float(d_map[d]) - float(s_map[d])) / float(s_map[d]) * 100.0
        for d in common
        if float(s_map[d]) > 0
    ]
    max_d = max(diffs) if diffs else 0.0
    med_d = statistics.median(diffs) if diffs else 0.0
    is_flagged = max_d > tol
    st = f"FLAG (> {tol}%)" if is_flagged else "OK"
    return [sym, str(len(common)), f"{max_d:.2f}%", f"{med_d:.2f}%", st], is_flagged, []


@actions_app.command("audit")
@handle_cli_errors
def actions_audit(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Symbol, ID, or @group selector"),
    from_date: str = typer.Option(..., "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str = typer.Option(..., "--to", help="End date (YYYY-MM-DD)"),
    exchange: str = typer.Option("NSE", "--exchange", help="Exchange code"),
) -> None:
    """Audit derived adjusted series vs source-adjusted series for symbols."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        symbols = _resolve_action_symbols(rt, manifest, selector, exchange)
        if not symbols:
            typer.echo(f"No symbols found for selector '{selector}'.")
            return
        tol = float(rt.params.get("corp.audit_tolerance_pct", "0.5")) if rt.params else 0.5
        start_dt = _parse_data_cli_dt(from_date, is_end=False)
        end_dt = _parse_data_cli_dt(to_date, is_end=True)
        ds = _build_data_service(rt, "yfinance")
        rows: list[list[str]] = []
        flagged = 0
        all_missing_cmds: list[str] = []

        for sym in symbols:
            row, is_fl, missing = _audit_symbol(
                ds, sym, exchange, start_dt, end_dt, from_date, to_date, tol
            )
            rows.append(row)
            if is_fl:
                flagged += 1
            all_missing_cmds.extend(missing)

        typer.echo(format_table(["Symbol", "Bars", "Max Diff", "Median Diff", "Status"], rows))
        for cmd in all_missing_cmds:
            typer.echo(f"Missing series command: {cmd}")
        if flagged:
            typer.echo(
                f"\nAudit completed: {flagged} symbol(s) flagged exceeding tolerance {tol}%."
            )
        else:
            typer.echo(f"\nAudit completed clean: all symbols within {tol}% tolerance.")


@override_app.command("add")
@handle_cli_errors
def actions_override_add(
    ctx: typer.Context,
    symbol: str = typer.Option(..., "--symbol", "-s", help="Instrument symbol (e.g. RELIANCE)"),
    ex_date: str = typer.Option(..., "--ex-date", "-d", help="Ex-date in YYYY-MM-DD format"),
    type: str = typer.Option(..., "--type", "-t", help="Action type (split, bonus, dividend)"),
    reason: str = typer.Option(..., "--reason", "-r", help="Operator rationale (required)"),
    source: str = typer.Option("operator", "--source", help="Data source party"),
    verified_on: str | None = typer.Option(None, "--verified-on", help="Verification date"),
    ratio: float | None = typer.Option(None, "--ratio", help="Adjustment ratio"),
    ratio_old: float | None = typer.Option(None, "--ratio-old", help="Old ratio value"),
    ratio_new: float | None = typer.Option(None, "--ratio-new", help="New ratio value"),
    amount: str | None = typer.Option(None, "--amount", help="Dividend cash amount"),
    issue_price: str | None = typer.Option(None, "--issue-price", help="Rights issue price"),
    exchange: str = typer.Option("NSE", "--exchange", help="Exchange code"),
) -> None:
    """Add a verified operator override to config/corporate_actions_overrides.toml."""
    if not reason or not reason.strip():
        raise typer.BadParameter("--reason must not be empty")

    with _open_rt(ctx, ensure=False) as rt:
        root = _get_project_root(ctx)
        ov = ActionOverride(
            symbol=symbol.strip().upper(),
            exchange=exchange.strip().upper(),
            ex_date=date.fromisoformat(ex_date.strip()[:10]),
            type=type.strip().lower(),
            source=source.strip(),
            reason=reason.strip(),
            verified_on=verified_on.strip() if verified_on else date.today().isoformat(),
            ratio=ratio,
            ratio_old=ratio_old,
            ratio_new=ratio_new,
            amount=Decimal(amount.strip()) if amount is not None else None,
            issue_price=Decimal(issue_price.strip()) if issue_price is not None else None,
        )
        add_override(ov, root / "config" / "corporate_actions_overrides.toml")
        j_payload: dict[str, Any] = {
            "symbol": ov.symbol,
            "exchange": ov.exchange,
            "type": ov.type,
            "source": ov.source,
            "verified_on": ov.verified_on,
            "reason": ov.reason,
            "ratio": ov.ratio,
            "ex_date": ov.ex_date.isoformat(),
            "amount": str(ov.amount) if ov.amount is not None else None,
            "issue_price": str(ov.issue_price) if ov.issue_price is not None else None,
        }
        _journal_data_event(rt, "data.override", j_payload, "data_override")
        typer.echo(f"Added override for {ov.symbol} on {ov.ex_date.isoformat()} ({ov.type}).")
