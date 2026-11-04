"""CLI commands for market closure inference, verification, and disagreement checking."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import typer

from algodesk.api.kinds import Mode, PluginKind
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.data.closures import (
    check_market_closure_disagreements,
    infer_market_closures,
)
from algodesk.data.manifest import DataManifest
from algodesk.ops.runtime import Runtime

closures_app = typer.Typer(
    name="closures",
    help="Market closure inference, verification, and disagreement checks.",
)


def _get_manifest(rt: Runtime) -> DataManifest:
    data_dir = rt.paths.data
    m_path = data_dir / "state" / "data_manifest.db"
    if not m_path.exists() and (data_dir / "manifest.db").exists():
        m_path = data_dir / "manifest.db"
    return DataManifest(m_path)


def _parse_date(val: str, param_name: str) -> date:
    val_clean = val.strip()
    try:
        return datetime.strptime(val_clean, "%Y-%m-%d").date()
    except ValueError:
        raise typer.BadParameter(
            f"Invalid date '{val_clean}'. Use YYYY-MM-DD format (e.g. 2026-11-03)",
            param_hint=param_name,
        ) from None


@closures_app.command("infer")
@handle_cli_errors
def closures_infer(
    ctx: typer.Context,
    exchange: str = typer.Option("NSE", "--exchange", help="Exchange code (e.g. NSE, BSE, MCX)"),
    from_date: str = typer.Option(..., "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str = typer.Option(..., "--to", help="End date (YYYY-MM-DD)"),
    sample: str | None = typer.Option(None, "--sample", help="Comma-separated sample symbols"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Infer market closures and special sessions from liquid sample symbols."""
    start_d = _parse_date(from_date, "--from")
    end_d = _parse_date(to_date, "--to")
    if start_d > end_d:
        raise typer.BadParameter("Start date must be <= end date", param_hint="--from")

    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        from algodesk.cli.data import _build_data_service
        from algodesk.cli.secrets import _resolve_secrets_provider
        from algodesk.ops.context import build_context

        # Read sample symbols and min symbols
        if sample:
            sample_symbols = [s.strip() for s in sample.split(",") if s.strip()]
        else:
            default_sample = (
                "NSE:EQ:RELIANCE,NSE:EQ:TCS,NSE:EQ:INFY,NSE:EQ:HDFCBANK,NSE:EQ:ICICIBANK"
            )
            sample_param = (
                str(rt.params.get("data.closure_sample", default_sample))
                if rt.params
                else default_sample
            )
            sample_symbols = [s.strip() for s in sample_param.split(",") if s.strip()]

        target_min_symbols = 1
        if rt.params:
            try:
                target_min_symbols = int(rt.params.get("data.closure_min_symbols"))
            except Exception:
                pass

        data_service = _build_data_service(rt)
        p_ctx = build_context(
            Mode.RESEARCH, run_id="closures_infer", secrets=_resolve_secrets_provider(rt)
        )
        cal: Any = None
        try:
            cal = rt.plugins.create(
                PluginKind.CALENDAR,
                "exchange",
                config={"exchange": exchange.lower()},
                ctx=p_ctx,
            )
        except Exception:
            pass

        records = infer_market_closures(
            manifest=manifest,
            data_service=data_service,
            exchange=exchange,
            start_d=start_d,
            end_d=end_d,
            sample_symbols=sample_symbols,
            min_symbols=target_min_symbols,
            calendar=cal,
        )

        inferred_cnt = sum(1 for r in records if r.kind == "inferred")
        special_cnt = sum(1 for r in records if r.kind == "special")

        if json_out:
            out_data = [
                {
                    "exchange": r.exchange,
                    "day": r.day,
                    "kind": r.kind,
                    "evidence": r.evidence,
                    "created": r.created,
                }
                for r in records
            ]
            typer.echo(json.dumps(out_data, indent=2))
            return

        if not records:
            typer.echo(
                f"No market closures or special sessions inferred for {exchange.upper()} "
                f"between {start_d} and {end_d}."
            )
            return

        headers = ["EXCHANGE", "DAY", "KIND", "EVIDENCE"]
        rows = [[r.exchange, r.day, r.kind, r.evidence] for r in records]
        typer.echo(format_table(headers, rows))
        typer.echo(f"Inferred: {inferred_cnt} closed (inferred), {special_cnt} special session(s).")


@closures_app.command("check")
@handle_cli_errors
def closures_check(
    ctx: typer.Context,
    exchange: str = typer.Option("NSE", "--exchange", help="Exchange code (e.g. NSE, BSE, MCX)"),
    from_date: str | None = typer.Option(None, "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str | None = typer.Option(None, "--to", help="End date (YYYY-MM-DD)"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Check for disagreements between calendar rules and recorded closures / data evidence."""
    start_d = _parse_date(from_date, "--from") if from_date else None
    end_d = _parse_date(to_date, "--to") if to_date else None
    if start_d and end_d and start_d > end_d:
        raise typer.BadParameter("Start date must be <= end date", param_hint="--from")

    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        from algodesk.cli.secrets import _resolve_secrets_provider
        from algodesk.ops.context import build_context

        p_ctx = build_context(
            Mode.RESEARCH, run_id="closures_check", secrets=_resolve_secrets_provider(rt)
        )
        cal: Any = None
        try:
            cal = rt.plugins.create(
                PluginKind.CALENDAR,
                "exchange",
                config={"exchange": exchange.lower()},
                ctx=p_ctx,
            )
        except Exception:
            pass

        disagreements = check_market_closure_disagreements(
            manifest=manifest,
            calendar=cal,
            exchange=exchange,
            start_d=start_d,
            end_d=end_d,
        )

        if json_out:
            typer.echo(json.dumps(disagreements, indent=2))
            return

        if not disagreements:
            typer.echo(f"No market closure disagreements found for {exchange.upper()}.")
            return

        headers = ["EXCHANGE", "DAY", "CALENDAR", "DATA / EVIDENCE", "DISAGREEMENT TYPE"]
        rows = [
            [
                d["exchange"],
                d["day"],
                d["calendar"],
                d["data_evidence"],
                d["disagreement_type"],
            ]
            for d in disagreements
        ]
        typer.echo(format_table(headers, rows))
        missing_h = sum(1 for d in disagreements if "missing holiday" in d["disagreement_type"])
        wrong_h = sum(
            1
            for d in disagreements
            if (
                "special session" in d["disagreement_type"]
                or "wrong holiday" in d["disagreement_type"]
            )
        )
        typer.echo(
            f"Total {len(disagreements)} closure disagreement(s): "
            f"{missing_h} missing holiday(s), {wrong_h} wrong holiday(s) or special session(s)."
        )


@closures_app.command("list")
@handle_cli_errors
def closures_list(
    ctx: typer.Context,
    exchange: str = typer.Option("NSE", "--exchange", help="Exchange code (e.g. NSE, BSE, MCX)"),
    from_date: str | None = typer.Option(None, "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str | None = typer.Option(None, "--to", help="End date (YYYY-MM-DD)"),
    kind: str | None = typer.Option(
        None, "--kind", help="Kind filter (inferred, calendar, special)"
    ),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List recorded market closures from manifest."""
    start_d = _parse_date(from_date, "--from") if from_date else None
    end_d = _parse_date(to_date, "--to") if to_date else None

    with _open_rt(ctx, ensure=False) as rt:
        manifest = _get_manifest(rt)
        records = manifest.list_market_closures(
            exchange=exchange,
            start_day=start_d.isoformat() if start_d else None,
            end_day=end_d.isoformat() if end_d else None,
            kind=kind,
        )

        if json_out:
            out_data = [
                {
                    "exchange": r.exchange,
                    "day": r.day,
                    "kind": r.kind,
                    "evidence": r.evidence,
                    "created": r.created,
                }
                for r in records
            ]
            typer.echo(json.dumps(out_data, indent=2))
            return

        if not records:
            typer.echo(f"No market closures found for {exchange.upper()}.")
            return

        headers = ["EXCHANGE", "DAY", "KIND", "EVIDENCE"]
        rows = [[r.exchange, r.day, r.kind, r.evidence] for r in records]
        typer.echo(format_table(headers, rows))
