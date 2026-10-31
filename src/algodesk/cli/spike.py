"""CLI spike commands to inspect live/research broker data characteristics."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.table import Table

from algodesk.api.kinds import Mode
from algodesk.api.market import Bar, InstrumentId, SeriesKey
from algodesk.cli.common import handle_cli_errors
from algodesk.ops.context import build_context
from algodesk.ops.runtime import Runtime

spike_app = typer.Typer(
    name="spike",
    help="Spike commands to probe and inspect live/research broker data characteristics.",
    no_args_is_help=True,
)


def _open_rt(ctx: typer.Context) -> Runtime:
    from algodesk.cli import common

    return common._open_rt(ctx, ensure=False)


def _write_spike_file(root: Path, file_name: str, content: str) -> Path:
    target_dir = root / "_workbench"
    target_dir.mkdir(parents=True, exist_ok=True)
    out_file = target_dir / file_name
    out_file.write_text(content, encoding="utf-8")
    return out_file


def _analyze_candles(bars: Sequence[Bar]) -> dict[str, Any]:
    if not bars:
        return {
            "first": "N/A",
            "last": "N/A",
            "count": 0,
            "months": {},
        }
    months: dict[str, int] = defaultdict(int)
    for b in bars:
        m_key = b.start_ts.strftime("%Y-%m")
        months[m_key] += 1
    return {
        "first": bars[0].start_ts.isoformat(),
        "last": bars[-1].start_ts.isoformat(),
        "count": len(bars),
        "months": dict(sorted(months.items())),
    }


def _check_split_discontinuity(
    bars_1d: Sequence[Bar],
    split_date_str: str | None,
) -> tuple[bool, str]:
    if not split_date_str or not bars_1d:
        return False, "Not evaluated (no split date provided)"
    try:
        ist = ZoneInfo("Asia/Kolkata")
        s_date = datetime.fromisoformat(split_date_str).date()
    except Exception:
        return False, f"Invalid split date format '{split_date_str}'"

    before_bar: Bar | None = None
    after_bar: Bar | None = None
    for b in bars_1d:
        b_date = b.start_ts.astimezone(ist).date()
        if b_date < s_date:
            before_bar = b
        elif b_date >= s_date and after_bar is None:
            after_bar = b

    if before_bar is None or after_bar is None:
        return False, f"Insufficient bars around split date {split_date_str}"

    ratio = after_bar.close / before_bar.close if before_bar.close > 0.0 else 1.0
    # Discontinuity if ratio deviates significantly from 1.0 (e.g. 2:1 split -> ~0.5)
    has_disc = ratio < 0.75 or ratio > 1.35
    msg = (
        f"Discontinuity detected (close before={before_bar.close:.2f}, "
        f"after={after_bar.close:.2f}, ratio={ratio:.4f})"
        if has_disc
        else f"Smooth continuous price curve (ratio={ratio:.4f})"
    )
    return has_disc, msg


def _print_history_results(
    source: str,
    symbol: str,
    analysis_1d: dict[str, Any],
    analysis_1m: dict[str, Any],
    req_counts: int,
    split_info: str,
) -> None:
    console = Console()
    table = Table(title=f"Spike {source.capitalize()} History: {symbol}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("1d First Candle", str(analysis_1d["first"]))
    table.add_row("1d Last Candle", str(analysis_1d["last"]))
    table.add_row("1d Total Bars", str(analysis_1d["count"]))
    table.add_row("1m First Candle", str(analysis_1m["first"]))
    table.add_row("1m Last Candle", str(analysis_1m["last"]))
    table.add_row("1m Total Bars", str(analysis_1m["count"]))
    table.add_row("Total Requests", str(req_counts))
    table.add_row("Split Check", split_info)
    console.print(table)


@spike_app.command("upstox-history")
@handle_cli_errors
def spike_upstox_history(
    ctx: typer.Context,
    symbol: str = typer.Option(
        ..., "--symbol", "-s", help="Symbol to inspect (e.g. RELIANCE, INFY)"
    ),
    split_date: str | None = typer.Option(
        None, "--split-date", help="Known stock split date (YYYY-MM-DD) to test price discontinuity"
    ),
) -> None:
    """Probe Upstox History V3 for 1m and 1d historical candle coverage and split adjustments."""
    with _open_rt(ctx) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"spike_upstox_{symbol}")
        broker: Any = rt.plugins.create("broker", "upstox", config={}, ctx=p_ctx)

        now_dt = datetime.now(UTC)
        start_1d = now_dt - timedelta(days=365)
        if split_date:
            try:
                sd = datetime.fromisoformat(split_date).replace(tzinfo=UTC)
                if sd < start_1d:
                    start_1d = sd - timedelta(days=30)
            except Exception:
                pass
        start_1m = now_dt - timedelta(days=30)

        inst_id = InstrumentId(symbol if ":" in symbol else f"NSE:EQ:{symbol}")
        k_1d = SeriesKey(inst_id, "bars", "1d", "adjusted", "upstox")
        k_1m = SeriesKey(inst_id, "bars", "1m", "adjusted", "upstox")

        bars_1d = list(broker.history(k_1d, start_1d, now_dt))
        bars_1m = list(broker.history(k_1m, start_1m, now_dt))
        requests_count = 2

        a_1d = _analyze_candles(bars_1d)
        a_1m = _analyze_candles(bars_1m)
        _, split_msg = _check_split_discontinuity(bars_1d, split_date)

        _print_history_results("upstox", symbol, a_1d, a_1m, requests_count, split_msg)

        report = (
            f"# Upstox History Spike Report: {symbol}\n\n"
            "## Summary\n"
            f"Spike inspection of Upstox History V3 historical data coverage for symbol {symbol}. "
            "Evaluates 1-day and 1-minute candle boundaries, request counts, sampled monthly "
            "candle counts, and checks whether price discontinuity occurs across stock splits.\n\n"
            "## Coverage Metrics\n"
            f"- Symbol: {symbol}\n"
            f"- 1d First: {a_1d['first']} | Last: {a_1d['last']} | Rows: {a_1d['count']}\n"
            f"- 1m First: {a_1m['first']} | Last: {a_1m['last']} | Rows: {a_1m['count']}\n"
            f"- Requests: {requests_count}\n"
            f"- Split Verification: {split_msg}\n\n"
            "## Sampled Monthly Counts (1d)\n"
            + "\n".join(f"- {m}: {cnt} rows" for m, cnt in a_1d["months"].items())
            + "\n"
        )
        out = _write_spike_file(rt.root, "spike_upstox-history.md", report)
        typer.echo(f"Report written to {out}")


@spike_app.command("yfinance-history")
@handle_cli_errors
def spike_yfinance_history(
    ctx: typer.Context,
    symbol: str = typer.Option(
        ..., "--symbol", "-s", help="Symbol to inspect (e.g. RELIANCE, INFY)"
    ),
    split_date: str | None = typer.Option(
        None, "--split-date", help="Known stock split date (YYYY-MM-DD) to test price discontinuity"
    ),
) -> None:
    """Probe Yahoo Finance for 1m and 1d historical candle coverage and split adjustments."""
    with _open_rt(ctx) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"spike_yfinance_{symbol}")
        broker: Any = rt.plugins.create("broker", "yfinance", config={}, ctx=p_ctx)

        now_dt = datetime.now(UTC)
        start_1d = now_dt - timedelta(days=365)
        if split_date:
            try:
                sd = datetime.fromisoformat(split_date).replace(tzinfo=UTC)
                if sd < start_1d:
                    start_1d = sd - timedelta(days=30)
            except Exception:
                pass
        start_1m = now_dt - timedelta(days=7)

        inst_id = InstrumentId(symbol if ":" in symbol else f"NSE:EQ:{symbol}")
        k_1d = SeriesKey(inst_id, "bars", "1d", "adjusted", "yfinance")
        k_1m = SeriesKey(inst_id, "bars", "1m", "adjusted", "yfinance")

        bars_1d = list(broker.history(k_1d, start_1d, now_dt))
        bars_1m = list(broker.history(k_1m, start_1m, now_dt))
        requests_count = 2

        a_1d = _analyze_candles(bars_1d)
        a_1m = _analyze_candles(bars_1m)
        _, split_msg = _check_split_discontinuity(bars_1d, split_date)

        _print_history_results("yfinance", symbol, a_1d, a_1m, requests_count, split_msg)

        report = (
            f"# Yahoo Finance History Spike Report: {symbol}\n\n"
            "## Summary\n"
            f"Spike inspection of Yahoo Finance historical data coverage for symbol {symbol}. "
            "Evaluates 1-day and 1-minute candle boundaries, request counts, sampled monthly "
            "candle counts, and checks whether price discontinuity occurs across stock splits.\n\n"
            "## Coverage Metrics\n"
            f"- Symbol: {symbol}\n"
            f"- 1d First: {a_1d['first']} | Last: {a_1d['last']} | Rows: {a_1d['count']}\n"
            f"- 1m First: {a_1m['first']} | Last: {a_1m['last']} | Rows: {a_1m['count']}\n"
            f"- Requests: {requests_count}\n"
            f"- Split Verification: {split_msg}\n\n"
            "## Sampled Monthly Counts (1d)\n"
            + "\n".join(f"- {m}: {cnt} rows" for m, cnt in a_1d["months"].items())
            + "\n"
        )
        out = _write_spike_file(rt.root, "spike_yfinance-history.md", report)
        typer.echo(f"Report written to {out}")


@spike_app.command("upstox-actions")
@handle_cli_errors
def spike_upstox_actions(
    ctx: typer.Context,
    symbols: str = typer.Option(
        ..., "--symbols", help="Comma-separated symbols to inspect (e.g. RELIANCE,INFY)"
    ),
) -> None:
    """Probe Upstox corporate actions response field names and events."""
    with _open_rt(ctx) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id="spike_upstox_actions")
        broker: Any = rt.plugins.create("broker", "upstox", config={}, ctx=p_ctx)

        sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
        lines: list[str] = []
        console = Console()
        table = Table(title="Upstox Corporate Actions Spike")
        table.add_column("Symbol", style="cyan")
        table.add_column("Fields", style="yellow")
        table.add_column("Events Count", style="green")

        for s in sym_list:
            res = broker.actions(s) if hasattr(broker, "actions") else {}
            fields = list(res.keys()) if isinstance(res, dict) else []
            ev_list = res.get("events", []) if isinstance(res, dict) else []
            ev_cnt = len(ev_list) if isinstance(ev_list, list) else 0
            f_str = ", ".join(fields)
            table.add_row(s, f_str, str(ev_cnt))
            lines.append(f"- **{s}**: fields=[{f_str}], events={ev_cnt}")

        console.print(table)
        report = (
            "# Upstox Corporate Actions Spike Report\n\n"
            "## Summary\n"
            "Inspection of Upstox corporate actions and event endpoints across probed "
            "symbols. Lists available field names and event records without disclosing tokens.\n\n"
            "## Results\n" + "\n".join(lines) + "\n"
        )
        out = _write_spike_file(rt.root, "spike_upstox-actions.md", report)
        typer.echo(f"Report written to {out}")
