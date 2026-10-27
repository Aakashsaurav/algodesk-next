"""Broker authentication and live market feed probing CLI commands."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from algodesk.api import InstrumentId, resolve_secret_ref
from algodesk.api.kinds import Mode
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.cli.secrets import _resolve_secrets_provider
from algodesk.ops.context import build_context

login_app = typer.Typer(help="Manage broker authentication and credentials.")
feed_app = typer.Typer(help="Probe and inspect live market data feeds.")


@login_app.command("upstox")
@handle_cli_errors
def login_upstox(
    ctx: typer.Context,
    manual: bool = typer.Option(False, "--manual", help="Force manual browser login URL flow"),
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
) -> None:
    """Authenticate with Upstox via automated loopback OAuth flow or manual URL."""
    with _open_rt(ctx, ensure=False) as rt:
        sec_prov = _resolve_secrets_provider(rt, run=run)
        p_ctx = build_context(Mode.RESEARCH, run_id="login_upstox", secrets=sec_prov)
        broker: Any = rt.plugins.create("broker", "upstox", config={}, ctx=p_ctx)
        if manual:
            req = broker.begin()
            typer.echo("Open the following URL in your browser to authorize AlgoDesk:")
            typer.echo(f"\n  {req.url}\n")
            code = typer.prompt("Enter authorization code from redirect URL")
            broker.complete(code.strip())
            typer.echo("Successfully authenticated with Upstox! Access token stored.")
        else:
            typer.echo("Opening browser for Upstox OAuth login...")
            try:
                broker.begin_auto()
                typer.echo("Successfully authenticated with Upstox! Access token stored.")
            except Exception as exc:
                typer.echo(f"Automated flow failed ({exc}). Falling back to manual flow...")
                req = broker.begin()
                typer.echo("Open the following URL in your browser to authorize AlgoDesk:")
                typer.echo(f"\n  {req.url}\n")
                code = typer.prompt("Enter authorization code from redirect URL")
                broker.complete(code.strip())
                typer.echo("Successfully authenticated with Upstox! Access token stored.")


@login_app.command("status")
@handle_cli_errors
def login_status(
    ctx: typer.Context,
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
) -> None:
    """Show authentication status and token validity for configured brokers."""
    with _open_rt(ctx, ensure=False) as rt:
        sec_prov = _resolve_secrets_provider(rt, run=run)
        p_ctx = build_context(Mode.RESEARCH, run_id="login_status", secrets=sec_prov)
        broker: Any = rt.plugins.create("broker", "upstox", config={}, ctx=p_ctx)

        headers = ["PROVIDER", "AUTH_TYPE", "CONFIGURED", "VALID", "EXPIRES"]
        rows: list[list[str]] = []

        # Analytics token check
        ana_tok = None
        try:
            ana_tok = resolve_secret_ref(broker._cfg.analytics_token, p_ctx.secrets)
        except Exception:
            pass
        if ana_tok:
            from algodesk.api import AuthSession

            sess = AuthSession(token=ana_tok)
            valid = broker.validate(sess)
            exp = broker.expires_at(sess)
            exp_str = exp.strftime("%Y-%m-%d") if exp else "never"
            rows.append(["upstox", "analytics", "yes", "valid" if valid else "invalid", exp_str])
        else:
            rows.append(["upstox", "analytics", "no", "-", "-"])

        # OAuth access token check
        oauth_tok = None
        try:
            oauth_tok = resolve_secret_ref(broker._cfg.access_token, p_ctx.secrets)
        except Exception:
            pass
        if oauth_tok:
            from algodesk.api import AuthSession

            sess = AuthSession(token=oauth_tok)
            valid = broker.validate(sess)
            exp = broker.expires_at(sess)
            exp_str = exp.strftime("%Y-%m-%d %H:%M") if exp else "unknown"
            rows.append(["upstox", "oauth", "yes", "valid" if valid else "invalid", exp_str])
        else:
            rows.append(["upstox", "oauth", "no", "-", "-"])

    typer.echo(format_table(headers, rows))


def _format_event_line(event: Any, depth: bool) -> list[str]:
    exch_ts = getattr(event, "exch_ts", None)
    recv_ts = getattr(event, "recv_ts", datetime.now(UTC))
    t_str = exch_ts.strftime("%H:%M:%S") if exch_ts else recv_ts.strftime("%H:%M:%S")
    ev_type = type(event).__name__
    inst = getattr(event, "instrument", "")
    if ev_type == "Quote":
        ltp = getattr(event, "ltp", "-")
        vol = getattr(event, "volume", "-")
        lines = [f"[{t_str}] QUOTE {inst} | LTP: {ltp} | Vol: {vol}"]
        if depth:
            bids = " | ".join(f"{b.price} x {b.qty}" for b in getattr(event, "bids", ()))
            asks = " | ".join(f"{a.price} x {a.qty}" for a in getattr(event, "asks", ()))
            lines.append(f"   Bids (L5): {bids or '-'}")
            lines.append(f"   Asks (L5): {asks or '-'}")
        return lines
    if ev_type == "Tick":
        ltp = getattr(event, "ltp", "-")
        qty = getattr(event, "ltq", "-")
        vol = getattr(event, "volume", "-")
        return [f"[{t_str}] TICK  {inst} | LTP: {ltp} | Qty: {qty} | Vol: {vol}"]
    if ev_type == "FeedStatus":
        return [f"[{t_str}] STATUS state={getattr(event, 'state', '-')}"]
    return []


def _track_latency(
    event: Any,
    latencies: dict[str, list[float]],
    gaps: dict[str, int],
    last_ts: dict[str, datetime],
) -> None:
    recv_ts = getattr(event, "recv_ts", datetime.now(UTC))
    inst_k = str(getattr(event, "instrument", ""))
    exch_ts = getattr(event, "exch_ts", None)
    if inst_k in latencies and exch_ts is not None:
        latencies[inst_k].append((recv_ts - exch_ts).total_seconds() * 1000.0)
        if inst_k in last_ts and (exch_ts - last_ts[inst_k]).total_seconds() > 5.0:
            gaps[inst_k] += 1
        last_ts[inst_k] = exch_ts


async def _run_feed_probe(
    broker: Any,
    inst_ids: list[InstrumentId],
    seconds: float,
    record: Path | None,
    live: bool = False,
    depth: bool = False,
) -> tuple[dict[str, list[float]], dict[str, int], list[list[str]], int]:
    import asyncio

    latencies: dict[str, list[float]] = {str(i): [] for i in inst_ids}
    gaps: dict[str, int] = {str(i): 0 for i in inst_ids}
    sample_rows: list[list[str]] = []
    rec_count, start_t = 0, datetime.now(UTC).timestamp()
    last_ts: dict[str, datetime] = {}
    if record is not None:
        record.mkdir(parents=True, exist_ok=True)

    stream_iter = broker.stream(inst_ids).__aiter__()
    while seconds <= 0 or (datetime.now(UTC).timestamp() - start_t) < seconds:
        t_out = (
            max(0.1, seconds - (datetime.now(UTC).timestamp() - start_t)) if seconds > 0 else 1.0
        )
        try:
            event = await asyncio.wait_for(stream_iter.__anext__(), timeout=t_out)
        except TimeoutError:
            continue
        except (StopAsyncIteration, asyncio.CancelledError):
            break

        _track_latency(event, latencies, gaps, last_ts)
        if live:
            for line in _format_event_line(event, depth):
                typer.echo(line)
        inst_k = str(getattr(event, "instrument", ""))
        if len(sample_rows) < 10 and inst_k:
            e_val = getattr(event, "exch_ts", None) or getattr(event, "recv_ts", None)
            t_str = e_val.strftime("%H:%M:%S") if isinstance(e_val, datetime) else "-"
            sample_rows.append(
                [t_str, inst_k, f"{getattr(event, 'ltp', '-')}", str(getattr(event, "volume", "-"))]
            )
        if record is not None:
            rec_count += 1
            (record / f"event_{rec_count:06d}.txt").write_text(str(event), encoding="utf-8")

    return latencies, gaps, sample_rows, rec_count


def _build_probe_summary(
    inst_ids: list[InstrumentId], lats: dict[str, list[float]], gaps: dict[str, int]
) -> list[list[str]]:
    import statistics

    rows: list[list[str]] = []
    for inst in inst_ids:
        k = str(inst)
        lat_list = lats.get(k, [])
        min_l = f"{min(lat_list):.1f}" if lat_list else "-"
        med_l = f"{statistics.median(lat_list):.1f}" if lat_list else "-"
        max_l = f"{max(lat_list):.1f}" if lat_list else "-"
        rows.append([k, str(len(lat_list)), min_l, med_l, max_l, str(gaps.get(k, 0))])
    return rows


class _FeedProbeCommand(typer.core.TyperCommand):
    """Custom command that defaults --record to data/probe when passed as a flag."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        new_args: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == "--record":
                new_args.append(arg)
                if i + 1 < len(args) and not args[i + 1].startswith("-"):
                    new_args.append(args[i + 1])
                    i += 2
                    continue
                new_args.append("data/probe")
                i += 1
                continue
            new_args.append(arg)
            i += 1
        return super().parse_args(ctx, new_args)


@feed_app.command("probe", cls=_FeedProbeCommand)
@handle_cli_errors
def feed_probe_cmd(
    ctx: typer.Context,
    symbols: str = typer.Option(..., "--symbols", help="Comma-separated symbols to probe"),
    seconds: float = typer.Option(5.0, "--seconds", help="Duration in seconds (0 for continuous)"),
    record: Path | None = typer.Option(
        None, "--record", help="Directory to save raw frames (defaults to data/probe/)"
    ),
    source: str = typer.Option("upstox", "--source", help="Data source broker plugin"),
    run: str | None = typer.Option(None, "--run", help="Run configuration name"),
    live: bool = typer.Option(False, "--live", help="Stream incoming events live to terminal"),
    depth: bool = typer.Option(
        False, "--depth", help="Include 5-level market depth for quote events"
    ),
) -> None:
    """Probe live market data feed latency, tick arrival, and gaps."""
    import asyncio

    raw_syms = [s.strip() for s in symbols.split(",") if s.strip()]
    if not raw_syms:
        raise typer.BadParameter("Symbols must not be empty", param_hint="--symbols")
    inst_ids = [InstrumentId(s if ":" in s else f"NSE:EQ:{s}") for s in raw_syms]

    with _open_rt(ctx, ensure=False) as rt:
        sec_prov = _resolve_secrets_provider(rt, run=run)
        p_ctx = build_context(Mode.RESEARCH, run_id="feed_probe", secrets=sec_prov)
        broker: Any = rt.plugins.create("broker", source, config={}, ctx=p_ctx)
        try:
            lats, gaps, samples, rec_count = asyncio.run(
                _run_feed_probe(broker, inst_ids, seconds, record, live=live, depth=depth)
            )
        except KeyboardInterrupt:
            typer.echo("\nInterrupted by user.")
            return

    headers = ["INSTRUMENT", "TICKS", "MIN_LAT_MS", "MED_LAT_MS", "MAX_LAT_MS", "GAPS"]
    typer.echo(format_table(headers, _build_probe_summary(inst_ids, lats, gaps)))
    if samples:
        typer.echo("\nSample rows:")
        typer.echo(format_table(["TIME", "INSTRUMENT", "LTP", "VOLUME"], samples))
    if record is not None:
        typer.echo(f"\nRecorded {rec_count} event(s) to {record}")
