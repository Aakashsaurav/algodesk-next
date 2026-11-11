"""Market data inspection, retrieval, deletion, and trash CLI commands."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import typer

from algodesk.api import ActionRecord, Bar, InstrumentId, SeriesKey
from algodesk.api.errors import ConfigError, DataError
from algodesk.api.kinds import Mode, PluginKind
from algodesk.api.protocols import DataSource
from algodesk.cli.closures import closures_app
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
from algodesk.data.detection import (
    PriceVerificationResult,
    detect_price_anomalies,
    verify_prices_for_actions,
)
from algodesk.data.groups import (
    get_group,
    list_group_details,
    resolve_group_universe,
)
from algodesk.data.layout import dataset_id_for_key, parse_bar_file_path
from algodesk.data.manifest import (
    CoverageDayRecord,
    DataManifest,
    DatasetRecord,
    FileRecord,
    PartRecord,
)
from algodesk.data.migration import (
    apply_migration,
    build_migration_plan,
    finalize_migration,
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
data_app.add_typer(closures_app, name="closures")


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
    manifest = _get_manifest(rt)
    store = BarStore(
        getattr(getattr(rt, "paths", None), "data", Path("data")),
        manifest,
        params=rt.params,
        journal=getattr(rt, "journal", None),
    )
    if source == "nse_bhavcopy":
        b_ref: Any = rt.plugins.create(PluginKind.REFERENCE, "nse_bhavcopy", config={}, ctx=p_ctx)
        b_ref._store = store
        b_ref._manifest = manifest
        broker: DataSource = cast(DataSource, b_ref)
    else:
        broker = cast(DataSource, rt.plugins.create("broker", source, config={}, ctx=p_ctx))
    manifests = {p.manifest.name: p.manifest for p in rt.plugins.list() if hasattr(p, "manifest")}
    j = getattr(rt, "journal", None)
    cal: Any = None
    try:
        cal = rt.plugins.create(
            PluginKind.CALENDAR,
            "exchange",
            config={"exchange": "nse"},
            ctx=p_ctx,
        )
    except Exception:
        pass
    sources_dict: dict[str, DataSource] = {source: broker}
    if source != "nse_bhavcopy":
        try:
            b_ref_aux: Any = rt.plugins.create(
                PluginKind.REFERENCE, "nse_bhavcopy", config={}, ctx=p_ctx
            )
            b_ref_aux._store = store
            b_ref_aux._manifest = manifest
            sources_dict["nse_bhavcopy"] = cast(DataSource, b_ref_aux)
        except Exception:
            pass
    return DataService(
        sources_dict,
        source,
        store,
        None,
        manifests=manifests,
        calendar=cal,
        params=rt.params,
        journal=j,
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
    refresh: bool = typer.Option(False, "--refresh", help="Force refetch of EMPTY days"),
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
            refresh=refresh,
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


@data_app.command("accumulate")
@handle_cli_errors
def data_accumulate(
    ctx: typer.Context,
    group: str = typer.Option("demo5", "--group", "-g", help="Instrument group name"),
    source: str = typer.Option("yfinance", "--source", "-s", help="Data source broker plugin"),
    days: int = typer.Option(7, "--days", "-d", help="Number of complete sessions to accumulate"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Display accumulation plan without fetching"
    ),
) -> None:
    """Fetch latest complete sessions for an instrument group into the store."""
    from algodesk.data.accumulate import execute_accumulation

    with _open_rt(ctx, ensure=False) as rt:
        report = execute_accumulation(
            group=group,
            source=source,
            days=days,
            runtime=rt,
            data_service=_build_data_service(rt, source) if not dry_run else None,
            dry_run=dry_run,
        )

        if dry_run:
            typer.echo(f"Accumulation Plan: group={group}, source={source}, target_sessions={days}")
            headers = ["INSTRUMENT", "TF", "TARGET", "STORED", "FETCHABLE", "LOST"]
            rows = [
                [
                    it.instrument,
                    it.timeframe,
                    str(len(it.target_sessions)),
                    str(len(it.stored_sessions)),
                    str(len(it.fetchable_sessions)),
                    str(len(it.lost_sessions)),
                ]
                for it in report.plan_items
            ]
            typer.echo(format_table(headers, rows))
            typer.echo(
                f"\nSummary: {report.days_still_missing} session-timeframe(s) to fetch, "
                f"{report.days_lost_beyond_window} session(s) lost beyond retention window."
            )
            return

        typer.echo(
            f"Accumulation complete for group '{group}' ({source}):\n"
            f"  - Rows added: {report.rows_added}\n"
            f"  - Days still missing: {report.days_still_missing}\n"
            f"  - Days lost beyond source window: {report.days_lost_beyond_window}"
        )


@data_app.command("compare")
@handle_cli_errors
def data_compare(
    ctx: typer.Context,
    date_val: str = typer.Option(..., "--date", "-d", help="Trading date (YYYY-MM-DD)"),
    symbols: str | None = typer.Option(
        None, "--symbols", "-s", help="Comma-separated symbols to compare"
    ),
    source: str = typer.Option(
        "yfinance", "--source", help="Data source broker plugin to compare against"
    ),
) -> None:
    """Compare in-memory tick-built 1m bars against vendor source 1m bars."""
    from algodesk.data.compare import compare_ticks_with_source

    try:
        d = date.fromisoformat(date_val)
    except ValueError:
        raise typer.BadParameter("Invalid date format, expected YYYY-MM-DD") from None

    sym_list = [s.strip() for s in symbols.split(",")] if symbols else None
    with _open_rt(ctx, ensure=False) as rt:
        from algodesk.engine.bars import BarBuilder

        def _make_builder(on_close: Any) -> Any:
            return BarBuilder(timeframes=("1m",), fill_empty_bars=False, on_bar_closed=on_close)

        rep = compare_ticks_with_source(
            date_val=d,
            symbols=sym_list,
            source=source,
            runtime=rt,
            data_service=_build_data_service(rt, source),
            builder_factory=_make_builder,
        )
        headers = [
            "SYMBOL",
            "BUILT",
            "SOURCE",
            "MATCHED",
            "BUILT FIRST",
            "BUILT LAST",
            "SRC FIRST",
            "SRC LAST",
            "BUILT ONLY",
            "SRC ONLY",
            "MAX CLOSE DIFF",
            "MEDIAN CLOSE DIFF",
            "ABOVE TOL",
            "MAX VOL DIFF",
            "MEDIAN VOL DIFF",
        ]

        def _fmt_time(ts: datetime | None) -> str:
            if ts is None:
                return "-"
            return ts.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%H:%M")

        rows = [
            [
                r.symbol,
                str(r.built_count),
                str(r.source_count),
                str(r.matched_count),
                _fmt_time(r.built_first_ts),
                _fmt_time(r.built_last_ts),
                _fmt_time(r.source_first_ts),
                _fmt_time(r.source_last_ts),
                str(r.built_only_count),
                str(r.source_only_count),
                f"{r.max_close_diff:.4f}",
                f"{r.median_close_diff:.4f}",
                str(r.count_above_tolerance),
                str(r.max_volume_diff),
                f"{r.median_volume_diff:.2f}",
            ]
            for r in rep.results
        ]
        typer.echo(
            f"Comparison on {date_val} vs source '{source}' (tolerance: {rep.tolerance_pct}%):"
        )
        typer.echo(format_table(headers, rows))


@data_app.command("import")
@handle_cli_errors
def data_import(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Path to vendor CSV or Parquet file"),
    mapping: str = typer.Option(
        ..., "--mapping", "-m", help="Path to column mapping TOML or JSON file"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate and plan import without writing"
    ),
) -> None:
    """Import vendor market data file using column mapping definition."""
    from algodesk.data.importer import import_vendor_file

    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        store = BarStore(rt.paths.data, manifest, params=rt.params)
        validator = BarValidator(
            rt.paths.data, manifest, params=rt.params, journal=getattr(rt, "journal", None)
        )

        res = import_vendor_file(
            file_path=Path(path),
            mapping_path=Path(mapping),
            runtime=rt,
            store=store,
            validator=validator,
            dry_run=dry_run,
        )

        status_str = "Planned (dry-run)" if dry_run else "Imported"
        inst_summary = ", ".join(res.instruments)
        typer.echo(
            f"Successfully {status_str} {res.rows_count} {res.kind} row(s) for {inst_summary}:\n"
            f"  - Timeframe: {res.timeframe}\n"
            f"  - First timestamp: {res.first_ts}\n"
            f"  - Last timestamp: {res.last_ts}\n"
            f"  - File SHA-256: {res.sha256}"
        )


@data_app.command("smoke")
@handle_cli_errors
def data_smoke(
    ctx: typer.Context,
    symbols: str = typer.Option(
        "RELIANCE,TCS", "--symbols", "-s", help="Comma-separated symbols to smoke test"
    ),
) -> None:
    """Run smoke test verifying caching, zero-network repeated gets, gaps, and cross-source diff."""
    from algodesk.data.smoke import run_data_smoke

    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    with _open_rt(ctx, ensure=False) as rt:
        report = run_data_smoke(
            symbols=sym_list,
            runtime=rt,
            data_service_primary=_build_data_service(rt, "upstox"),
            data_service_secondary=_build_data_service(rt, "yfinance"),
        )

        headers = ["SYMBOL", "RUN2 REQS", "GAPS", "VERIFY", "MAX CLOSE DIFF (SRC2)"]
        rows = [
            [
                r.symbol,
                f"{r.network_requests_run2} (PASS)"
                if (r.network_requests_run2 == 0 or (report.overall_passed and r.partial_sessions))
                else f"{r.network_requests_run2} (FAIL)",
                str(r.gaps_count),
                "OK" if r.verify_passed else "FAIL",
                f"{r.max_close_diff_vs_source2:.4f}",
            ]
            for r in report.results
        ]
        typer.echo(
            f"Data Smoke Test Report ({report.primary_source} vs {report.secondary_source}):"
        )
        typer.echo(format_table(headers, rows))

        if report.partial_sessions:
            part_str = ", ".join(sorted(set(report.partial_sessions)))
            typer.echo(f"\nPartial sessions (current incomplete session): {part_str}")

        if report.run2_requests:
            typer.echo(f"\nRun 2 network requests ({len(report.run2_requests)} made):")
            for req in report.run2_requests:
                typer.echo(f"  - {req.get('key')} [{req.get('start')} to {req.get('end')}]")
        else:
            typer.echo("\nRun 2 made 0 network requests.")

        if report.overall_passed:
            typer.echo("\nSmoke check: PASS (zero network calls on repeated get, store verified)")
        else:
            typer.echo("\nSmoke check: FAIL")
            raise typer.Exit(code=1)


def _render_and_confirm_delete(
    plan: Any,
    kind: str | None,
    purge: bool,
    yes: bool,
    dry_run: bool = False,
) -> None:
    """Render delete plan table and prompt for confirmation."""
    headers = ["INSTRUMENT", "DATE", "KIND", "ROWS", "BYTES", "PATH"]
    table_rows = [
        [f.instrument, f.date, f.kind, str(f.rows), str(f.bytes), f.path] for f in plan.files
    ]
    if table_rows:
        typer.echo(format_table(headers, table_rows))

    for inst in plan.no_data_instruments:
        typer.echo(f"{inst}: no data")

    typer.echo(
        f"Plan: {plan.total_files} file(s) across {len(plan.instruments)} instrument(s) "
        f"({plan.total_bytes} bytes, {plan.total_rows} rows)"
    )

    if dry_run or plan.total_files == 0:
        if plan.total_files == 0:
            typer.echo("No files found matching selector(s).")
        return

    has_ticks = (kind == "ticks") or any(f.kind == "ticks" for f in plan.files)
    if has_ticks and purge:
        raise typer.BadParameter(
            "Ticks are irreplaceable and cannot be permanently purged bypassing trash."
        )

    if (kind == "ticks") or (not yes):
        expected = f"DELETE {plan.total_files} files"
        resp = typer.prompt(f"Type '{expected}' to confirm")
        if resp.strip() != expected:
            typer.echo("Deletion aborted.")
            raise typer.Exit(code=1)


@data_app.command("delete")
@handle_cli_errors
def data_delete(
    ctx: typer.Context,
    selectors: list[str] = typer.Argument(..., help="Symbol, ID, glob, or @group selector(s)"),
    from_date: str | None = typer.Option(None, "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str | None = typer.Option(None, "--to", help="End date (YYYY-MM-DD)"),
    older_than: int | None = typer.Option(
        None, "--older-than", help="Delete files older than N days"
    ),
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

        if older_than is not None:
            if older_than < 0:
                raise typer.BadParameter("--older-than must be non-negative")
            from datetime import date, timedelta

            cutoff_date = (date.today() - timedelta(days=older_than)).isoformat()
            if to_date is None:
                to_date = cutoff_date

        plan = plan_delete(
            selectors,
            manifest,
            from_date=from_date,
            to_date=to_date,
            kind=kind,
            groups_dir=groups_dir,
        )
        if dry_run or plan.total_files == 0:
            _render_and_confirm_delete(plan, kind, purge, yes=True, dry_run=dry_run)
            return

        _render_and_confirm_delete(plan, kind, purge, yes, dry_run=False)
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
    resolved: bool = typer.Option(
        False, "--resolved", help="Show resolved canonical instruments and content hash"
    ),
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

        if resolved:
            canonical_members, content_hash = resolve_group_universe(name, groups_dir)
            if json_out:
                typer.echo(
                    json.dumps(
                        {"name": name, "hash": content_hash, "members": canonical_members},
                        indent=2,
                    )
                )
                return
            m_lines = "\n".join(f"  - {m}" for m in canonical_members)
            typer.echo(f"Group: {name} (Resolved Hash: {content_hash})\nMembers:\n{m_lines}")
            return

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


@data_app.command("migrate-ids")
@handle_cli_errors
def migrate_ids(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List migration plan without modifying files"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Copy files to new partitions, verify, and update manifest"
    ),
    finalize: bool = typer.Option(
        False, "--finalize", help="Remove old legacy files after verified apply"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    json_opt: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
) -> None:
    """Migrate legacy instrument partition layout to canonical structured format."""
    with _open_rt(ctx, ensure=False) as rt:
        from algodesk.cli.common import _get_cli_state

        manifest = _get_manifest(rt)
        data_dir = rt.paths.data
        plan = build_migration_plan(data_dir, manifest)

        state = _get_cli_state(ctx)
        is_json = json_opt or state.json
        if is_json:
            typer.echo(json.dumps(plan.to_dict(), indent=2))
            return

        if finalize:
            if plan.total_files == 0:
                typer.echo("No legacy files to finalize.")
                return
            if not yes:
                confirm = typer.prompt("Type 'FINALIZE' to permanently delete old legacy files")
                if confirm != "FINALIZE":
                    typer.echo("Aborted.")
                    return
            count = finalize_migration(plan, manifest, journal=getattr(rt, "journal", None))
            typer.echo(f"Finalized migration for {count} file(s). Old legacy files removed.")
            return

        if apply:
            if plan.total_files == 0:
                typer.echo("No legacy files to migrate.")
                return
            if not yes:
                confirm = typer.prompt("Type 'yes' to proceed with migration copy")
                if confirm != "yes":
                    typer.echo("Aborted.")
                    return
            count = apply_migration(plan, manifest, journal=getattr(rt, "journal", None))
            typer.echo(
                f"Successfully copied and verified {count} file(s). "
                "Manifest updated with aliases. Run --finalize to remove old copies."
            )
            return

        if plan.total_files == 0:
            typer.echo("No legacy files to migrate. All files conform to canonical partitions.")
            return

        typer.echo(
            f"Migration Plan ({plan.total_files} file(s), "
            f"{plan.total_rows} rows, {plan.total_bytes} bytes):"
        )
        for item in plan.items:
            typer.echo(f"  Legacy:    {item.legacy_id}")
            typer.echo(f"  Canonical: {item.canonical_id}")
            typer.echo(f"  Source:    {item.source_path}")
            typer.echo(f"  Target:    {item.target_path}")
            typer.echo(f"  Rows:      {item.rows} (SHA: {item.sha256[:12]}...)")
            typer.echo("  ---")
        typer.echo("Plan listed. Dry-run mode: no files changed.")


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

        cal: Any = None
        try:
            sec_prov = _resolve_secrets_provider(rt)
            p_ctx = build_context(Mode.RESEARCH, run_id="data_gaps", secrets=sec_prov)
            cal = rt.plugins.create(
                PluginKind.CALENDAR,
                "exchange",
                config={"exchange": "nse"},
                ctx=p_ctx,
            )
        except Exception:
            pass

        cov_index = CoverageIndex(manifest=manifest, calendar=cal)
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

        closure_counts = cov_index.last_closure_counts
        closed_inf = closure_counts["closed_inferred"]
        total_days = (end_d - start_d).days + 1
        inf_msg = f"{closed_inf} day(s) relied on inferred closures."
        warning_msg = ""
        if total_days > 0 and (closed_inf / total_days) > 0.05:
            pct = (closed_inf / total_days) * 100.0
            warning_msg = (
                f"Warning: {closed_inf} days ({pct:.1f}%) relied on inferred closures, "
                f"exceeding 5% threshold."
            )

        counts_parts = [
            (
                f"Closures: {closure_counts['closed_calendar']} closed (calendar), "
                f"{closure_counts['closed_inferred']} closed (inferred), "
                f"{closure_counts['special_sessions']} special session(s)."
            ),
            inf_msg,
        ]
        if warning_msg:
            counts_parts.append(warning_msg)
        counts_msg = "\n".join(counts_parts)

        if json_out:
            typer.echo(json.dumps(all_gaps, indent=2))
            return

        if not all_gaps:
            typer.echo(f"No gaps found for '{selector}' between {start_d} and {end_d}.")
            typer.echo(counts_msg)
            return

        headers = ["INSTRUMENT", "DAY (IST)", "STATUS", "REASON"]
        rows = [
            [g["instrument"], format_ist(g["day"], date_only=True), g["status"], g["reason"]]
            for g in all_gaps
        ]
        typer.echo(format_table(headers, rows))
        typer.echo(counts_msg)


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
    req = requested_source.strip().lower() if requested_source else "all"
    if req == "bse":
        raise DataError(
            what=(
                "BSE corporate actions endpoint is blocked by exchange bot protection "
                "(Akamai HTTP 403 Access Denied)"
            ),
            where="CorporateActionsManager._build_action_sources",
            fix="BSE automated access is blocked; choose from nse, upstox, yfinance, or all",
        )
    names = ["upstox", "yfinance", "nse"] if req == "all" else [req]
    for name in names:
        try:
            if name in ("nse", "nse_actions"):
                p = rt.plugins.create(PluginKind.REFERENCE, "nse_actions", config={}, ctx=p_ctx)
                sources[name] = p
            elif name in ("upstox", "yfinance"):
                sources[name] = rt.plugins.create(PluginKind.BROKER, name, config={}, ctx=p_ctx)
        except Exception:
            if requested_source and requested_source.strip().lower() != "all":
                raise
    return sources


@actions_app.command("refresh")
@handle_cli_errors
def actions_refresh(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Symbol, ID, or @group selector"),
    source: str = typer.Option("all", "--source", help="Data source: nse|bse|upstox|yfinance|all"),
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
        parts = [s.strip() for s in selector.split(",") if s.strip()]
        syms: list[str] = []
        for part in parts:
            try:
                res = resolve_selector(part, manifest, groups_dir=Path("config/groups"))
                syms.extend([str(it).split(":")[-1].upper() for it in res])
            except Exception:
                syms.append(part.upper())
        return list(dict.fromkeys(syms)) or [s.upper() for s in parts]
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


def _format_summary_counts(statuses: Sequence[str], full_taxonomy: bool = False) -> str:
    from collections import Counter

    counts = Counter(statuses)
    if full_taxonomy:
        categories = [
            "confirmed",
            "confirmed (implied)",
            "unverified",
            "conflict",
            "manual_required",
        ]
        extra = sorted([k for k in counts if k not in categories])
        all_keys = categories + extra
        return ", ".join(f"{st}={counts.get(st, 0)}" for st in all_keys)
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


COMPARE_COLUMNS = ["Symbol", "Date", "Type", "Sources", "Values", "Evidence", "Status", "Reason"]


def _format_event_values(acts: Sequence[ActionRecord], later_factor: float = 1.0) -> str:
    parts: list[str] = []
    for a in sorted(acts, key=lambda x: x.source.lower()):
        src = a.source or "unknown"
        if a.amount is not None:
            if src.lower() != "yfinance" and later_factor != 1.0:
                norm_val = float(a.amount) / later_factor
                v = f"Rs {a.amount} (norm: {norm_val:.4g})"
            else:
                v = f"Rs {a.amount}"
        elif a.ratio is not None:
            if a.ratio_new is not None and a.ratio_old is not None:
                v = f"{a.ratio_new:g}:{a.ratio_old:g}"
            else:
                v = (
                    f"{a.ratio:g}"
                    if isinstance(a.ratio, float) and a.ratio.is_integer()
                    else f"{a.ratio}"
                )
        else:
            v = "-"
        parts.append(f"{src}: {v}")
    return ", ".join(parts) if parts else "-"


@actions_app.command("compare")
@handle_cli_errors
def actions_compare(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Symbol, ID, or @group selector"),
    exchange: str = typer.Option("NSE", "--exchange", help="Exchange code"),
) -> None:
    """Compare corporate action sources per event with values, status, and reason."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        store = ActionsStore(data_dir=rt.paths.data, manifest=manifest)
        symbols = _resolve_action_symbols(rt, manifest, selector, exchange)
        if not symbols:
            typer.echo("No corporate actions found.")
            return

        reconciler = _build_reconciler(ctx, rt.params)
        rows: list[list[str]] = []
        for s in symbols:
            raw_actions = store.read(exchange, s)
            if not raw_actions:
                continue
            reconciled = reconciler.reconcile(exchange, s, raw_actions)
            split_bonus_factors = reconciler.get_split_bonus_factors(raw_actions)

            by_key: dict[tuple[date, str], list[ActionRecord]] = {}
            for a in raw_actions:
                by_key.setdefault((a.ex_date, a.type.lower()), []).append(a)

            for rec in reconciled:
                matching = by_key.get((rec.ex_date, rec.type.lower()))
                if not matching:
                    if rec.type.lower() == "bonus":
                        matching = by_key.get((rec.ex_date, "split")) or [rec]
                    else:
                        matching = [rec]
                srcs = ", ".join(sorted({a.source for a in matching if a.source}))
                later_factor = reconciler.cumulative_factor_after(split_bonus_factors, rec.ex_date)
                vals = _format_event_values(matching, later_factor=later_factor)
                ev_kind = reconciler.classify_evidence(rec, matching)
                rows.append(
                    [
                        rec.symbol,
                        rec.ex_date.isoformat(),
                        rec.type,
                        srcs or rec.source or "-",
                        vals,
                        ev_kind,
                        rec.status,
                        rec.reason or "-",
                    ]
                )

        if not rows:
            typer.echo("No corporate actions found.")
            return

        typer.echo(format_table(COMPARE_COLUMNS, rows))
        typer.echo(
            f"\nSummary counts: {_format_summary_counts([r[6] for r in rows], full_taxonomy=True)}"
        )


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


VERIFY_PRICES_COLUMNS = [
    "Date",
    "Symbol",
    "Type",
    "Declared",
    "Observed",
    "Verdict",
    "Details",
]


def _load_bars_for_verify(
    rt: Runtime,
    sym: str,
    exchange: str,
    start_dt: datetime,
    end_dt: datetime,
    source: str,
) -> list[Bar]:
    inst = f"{exchange.upper()}:EQ:{sym.upper()}"
    try:
        store = BarStore(rt.paths.data, _get_manifest(rt), params=rt.params)
        key = SeriesKey(InstrumentId(inst), "bars", "1d", "raw", source)
        s_slice = store.read(key, start_dt, end_dt)
        if s_slice.bars:
            return list(s_slice.bars)
    except Exception:
        pass

    try:
        ds = _build_data_service(rt, source=source)
        d_slice = ds.get(inst, start_dt, end_dt, download="raw", presence_only=True, source=source)
        if d_slice.bars:
            return list(d_slice.bars)
    except Exception:
        pass
    return []


@actions_app.command("verify-prices")
@handle_cli_errors
def actions_verify_prices(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Symbol, ID, or @group selector"),
    from_date: str = typer.Option(..., "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str = typer.Option(..., "--to", help="End date (YYYY-MM-DD)"),
    exchange: str = typer.Option("NSE", "--exchange", help="Exchange code"),
    source: str = typer.Option("nse_bhavcopy", "--source", help="Data source for raw closes"),
) -> None:
    """Verify declared splits and bonuses against raw closes, and flag unexplained jumps."""
    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        store = ActionsStore(data_dir=rt.paths.data, manifest=manifest)
        symbols = _resolve_action_symbols(rt, manifest, selector, exchange)
        if not symbols:
            typer.echo(f"No symbols found for selector '{selector}'.")
            return

        p = rt.params
        reconciler = _build_reconciler(ctx, p)
        raw_tol = (
            (
                p.get("corp.price_verify_tolerance_pct")
                or p.get("corp.price_verification_tolerance_pct")
                or 8.0
            )
            if p
            else 8.0
        )
        tol = float(raw_tol)
        raw_jump = p.get("corp.detect_jump_pct", "20.0") if p else 20.0
        jump_pct = float(raw_jump)

        start_dt = _parse_data_cli_dt(from_date, is_end=False)
        end_dt = _parse_data_cli_dt(to_date, is_end=True)

        all_results: list[PriceVerificationResult] = []
        for sym in symbols:
            raw_acts = store.read(exchange, sym)
            acts = reconciler.reconcile(exchange, sym, raw_acts)
            bars = _load_bars_for_verify(rt, sym, exchange, start_dt, end_dt, source)
            res_list = verify_prices_for_actions(
                sym, bars, acts, tolerance_pct=tol, jump_pct=jump_pct
            )
            all_results.extend(res_list)

        if not all_results:
            typer.echo("No corporate actions or price anomalies found in range.")
            return

        rows = [
            [
                r.date.isoformat(),
                r.symbol,
                r.event_type.upper(),
                f"{r.declared_factor:.4f}",
                f"{r.observed_factor:.4f}",
                r.verdict,
                r.details,
            ]
            for r in all_results
        ]
        typer.echo(format_table(VERIFY_PRICES_COLUMNS, rows))
        verdicts = [r.verdict for r in all_results]
        v_counts = {v: verdicts.count(v) for v in sorted(set(verdicts))}
        v_summary = ", ".join(f"{k}={v}" for k, v in v_counts.items())
        typer.echo(f"\nSummary counts: {v_summary}")


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
