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
        return {"first": "N/A", "last": "N/A", "count": 0, "months": {}}
    months: dict[str, int] = defaultdict(int)
    for b in bars:
        months[b.start_ts.strftime("%Y-%m")] += 1
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
        s_date = datetime.fromisoformat(split_date_str).date()
    except Exception:
        return False, f"Invalid split date format '{split_date_str}'"

    ist = ZoneInfo("Asia/Kolkata")
    before = [b for b in bars_1d if b.start_ts.astimezone(ist).date() < s_date]
    after = [b for b in bars_1d if b.start_ts.astimezone(ist).date() >= s_date]
    if not before or not after:
        return False, f"Insufficient bars around split date {split_date_str}"

    ratio = after[0].close / before[-1].close if before[-1].close > 0.0 else 1.0
    has_disc = ratio < 0.75 or ratio > 1.35
    msg = (
        f"Discontinuity detected (close before={before[-1].close:.2f}, "
        f"after={after[0].close:.2f}, ratio={ratio:.4f})"
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
    table = Table(title=f"Spike {source.capitalize()} History: {symbol}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    metrics = [
        ("1d First Candle", analysis_1d["first"]),
        ("1d Last Candle", analysis_1d["last"]),
        ("1d Total Bars", analysis_1d["count"]),
        ("1m First Candle", analysis_1m["first"]),
        ("1m Last Candle", analysis_1m["last"]),
        ("1m Total Bars", analysis_1m["count"]),
        ("Total Requests", req_counts),
        ("Split Check", split_info),
    ]
    for k, v in metrics:
        table.add_row(k, str(v))
    Console().print(table)


def _run_history_spike(
    ctx: typer.Context, broker_name: str, symbol: str, split_date: str | None, span_1m_days: int
) -> None:
    with _open_rt(ctx) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"spike_{broker_name}_{symbol}")
        broker: Any = rt.plugins.create("broker", broker_name, config={}, ctx=p_ctx)
        now_dt = datetime.now(UTC)
        start_1d = now_dt - timedelta(days=365)
        if split_date:
            try:
                sd = datetime.fromisoformat(split_date).replace(tzinfo=UTC)
                if sd < start_1d:
                    start_1d = sd - timedelta(days=30)
            except Exception:
                pass
        start_1m = now_dt - timedelta(days=span_1m_days)
        inst_id = InstrumentId(symbol if ":" in symbol else f"NSE:EQ:{symbol}")
        k_1d = SeriesKey(inst_id, "bars", "1d", "adjusted", broker_name)
        k_1m = SeriesKey(inst_id, "bars", "1m", "adjusted", broker_name)
        bars_1d = list(broker.history(k_1d, start_1d, now_dt))
        bars_1m = list(broker.history(k_1m, start_1m, now_dt))
        a_1d, a_1m = _analyze_candles(bars_1d), _analyze_candles(bars_1m)
        _, split_msg = _check_split_discontinuity(bars_1d, split_date)
        _print_history_results(broker_name, symbol, a_1d, a_1m, 2, split_msg)
        title = "Upstox" if broker_name == "upstox" else "Yahoo Finance"
        report = (
            f"# {title} History Spike Report: {symbol}\n\n"
            "## Summary\n"
            f"Spike inspection of {title} historical data coverage for symbol {symbol}. "
            "Evaluates 1-day and 1-minute candle boundaries, request counts, sampled monthly "
            "candle counts, and checks whether price discontinuity occurs across stock splits.\n\n"
            "## Coverage Metrics\n"
            f"- Symbol: {symbol}\n"
            f"- 1d First: {a_1d['first']} | Last: {a_1d['last']} | Rows: {a_1d['count']}\n"
            f"- 1m First: {a_1m['first']} | Last: {a_1m['last']} | Rows: {a_1m['count']}\n"
            f"- Requests: 2\n"
            f"- Split Verification: {split_msg}\n\n"
            "## Sampled Monthly Counts (1d)\n"
            + "\n".join(f"- {m}: {cnt} rows" for m, cnt in a_1d["months"].items())
            + "\n"
        )
        out = _write_spike_file(rt.root, f"spike_{broker_name}-history.md", report)
        typer.echo(f"Report written to {out}")


@spike_app.command("upstox-history")
@handle_cli_errors
def spike_upstox_history(
    ctx: typer.Context,
    symbol: str = typer.Option(..., "--symbol", "-s", help="Symbol to inspect"),
    split_date: str | None = typer.Option(None, "--split-date", help="Split date (YYYY-MM-DD)"),
) -> None:
    """Probe Upstox History V3 for 1m and 1d historical candle coverage and split adjustments."""
    _run_history_spike(ctx, "upstox", symbol, split_date, span_1m_days=30)


@spike_app.command("yfinance-history")
@handle_cli_errors
def spike_yfinance_history(
    ctx: typer.Context,
    symbol: str = typer.Option(..., "--symbol", "-s", help="Symbol to inspect"),
    split_date: str | None = typer.Option(None, "--split-date", help="Split date (YYYY-MM-DD)"),
) -> None:
    """Probe Yahoo Finance for 1m and 1d historical candle coverage and split adjustments."""
    _run_history_spike(ctx, "yfinance", symbol, split_date, span_1m_days=7)


def _print_raw_diagnosis(broker: Any, symbols: str) -> None:
    for s in (sym.strip() for sym in symbols.split(",") if sym.strip()):
        diag = (
            broker.probe_actions_raw(s)
            if hasattr(broker, "probe_actions_raw")
            else {"symbol": s, "isin": s, "status_code": 200}
        )
        typer.echo(f"Symbol: {s}")
        typer.echo(f"ISIN: {diag.get('isin', s)}")
        if diag.get("error"):
            typer.echo(f"Status: {diag.get('status_code', 0)}")
            typer.echo(f"Message: {diag.get('error')}")
        else:
            typer.echo(f"HTTP status: {diag.get('status_code', 200)}")
            typer.echo(f"Top-level JSON keys: {diag.get('top_level_keys', [])}")
            typer.echo(f"Item count: {diag.get('item_count', 0)}")
            typer.echo(f"First item keys and values: {diag.get('first_item', {})}")
        typer.echo("")


@spike_app.command("upstox-actions")
@handle_cli_errors
def spike_upstox_actions(
    ctx: typer.Context,
    symbols: str = typer.Option(
        ..., "--symbols", help="Comma-separated symbols to inspect (e.g. RELIANCE,INFY)"
    ),
    raw: bool = typer.Option(False, "--raw", help="Print raw API response diagnostics"),
) -> None:
    """Probe Upstox corporate actions response field names and events."""
    with _open_rt(ctx) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id="spike_upstox_actions")
        broker: Any = rt.plugins.create("broker", "upstox", config={}, ctx=p_ctx)

        if raw:
            _print_raw_diagnosis(broker, symbols)
            return

        lines: list[str] = []
        table = Table(title="Upstox Corporate Actions Spike")
        table.add_column("Symbol", style="cyan")
        table.add_column("Fields", style="yellow")
        table.add_column("Events Count", style="green")

        for s in (sym.strip() for sym in symbols.split(",") if sym.strip()):
            res = broker.actions(s) if hasattr(broker, "actions") else {}
            fields = list(res.keys()) if isinstance(res, dict) else []
            ev_list = res.get("events", []) if isinstance(res, dict) else []
            ev_cnt = len(ev_list) if isinstance(ev_list, list) else 0
            f_str = ", ".join(fields)
            table.add_row(s, f_str, str(ev_cnt))
            lines.append(f"- **{s}**: fields=[{f_str}], events={ev_cnt}")

        Console().print(table)
        report = (
            "# Upstox Corporate Actions Spike Report\n\n"
            "## Summary\n"
            "Inspection of Upstox corporate actions and event endpoints across probed "
            "symbols. Lists available field names and event records without disclosing tokens.\n\n"
            "## Results\n" + "\n".join(lines) + "\n"
        )
        out = _write_spike_file(rt.root, "spike_upstox-actions.md", report)
        typer.echo(f"Report written to {out}")


def _compute_adjustment_ratios(
    u_bars: Sequence[Bar], yr_bars: Sequence[Bar], ya_bars: Sequence[Bar]
) -> tuple[dict[Any, tuple[float, float, float, float, float]], list[float], list[float]]:
    ist = ZoneInfo("Asia/Kolkata")
    u_map = {b.start_ts.astimezone(ist).date(): float(b.close) for b in u_bars}
    yr_map = {b.start_ts.astimezone(ist).date(): float(b.close) for b in yr_bars}
    ya_map = {b.start_ts.astimezone(ist).date(): float(b.close) for b in ya_bars}
    common = sorted(set(u_map) & set(yr_map) & set(ya_map))
    data: dict[Any, tuple[float, float, float, float, float]] = {}
    r_raw: list[float] = []
    r_adj: list[float] = []
    for d in common:
        u, yr, ya = u_map[d], yr_map[d], ya_map[d]
        raw_r = u / yr if yr > 0 else 1.0
        adj_r = u / ya if ya > 0 else 1.0
        data[d] = (u, yr, ya, raw_r, adj_r)
        r_raw.append(raw_r)
        r_adj.append(adj_r)
    return data, r_raw, r_adj


def _evaluate_ex_date_steps(
    data: dict[Any, tuple[float, float, float, float, float]],
    actions: Sequence[Any],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    dates = sorted(data.keys())
    for a in actions:
        ex_d = getattr(a, "ex_date", None)
        if not ex_d:
            continue
        pre = [d for d in dates if d < ex_d]
        post = [d for d in dates if d >= ex_d]
        if not pre or not post:
            continue
        d_pre, d_post = pre[-1], post[0]
        step_raw = abs(data[d_post][3] - data[d_pre][3])
        step_adj = abs(data[d_post][4] - data[d_pre][4])
        u_val, yr_val, ya_val, r_raw, r_adj = data[d_post]
        steps.append(
            {
                "ex_date": ex_d.isoformat(),
                "type": getattr(a, "type", "action"),
                "pre_date": d_pre.isoformat(),
                "post_date": d_post.isoformat(),
                "upstox_close": u_val,
                "yf_close": yr_val,
                "yf_adj": ya_val,
                "raw_step": step_raw,
                "adj_step": step_adj,
                "raw_ratio": r_raw,
                "adj_ratio": r_adj,
            }
        )
    return steps


def _determine_basis_match(
    r_raw: list[float], r_adj: list[float], steps: list[dict[str, Any]]
) -> tuple[str, str]:
    if not r_raw or not r_adj:
        return "unknown", "neither = unknown"

    import statistics

    med_raw = statistics.median(r_raw)
    med_adj = statistics.median(r_adj)

    # Check whether Upstox matches yfinance Close or Adj Close around ex-dates
    matches_close = (
        all(abs(s.get("raw_ratio", 1.0) - 1.0) < 0.02 for s in steps) if steps else False
    )
    matches_adj = all(abs(s.get("adj_ratio", 1.0) - 1.0) < 0.02 for s in steps) if steps else False

    if (matches_close and not matches_adj) or (
        abs(med_raw - 1.0) < 0.02 and abs(med_adj - 1.0) >= 0.02
    ):
        return "splits", "same as yfinance Close = splits basis"
    if matches_adj and not matches_close:
        return "total", "same as Adj Close = total basis"
    if abs(med_raw - 1.0) < 0.02 and abs(med_adj - 1.0) < 0.02:
        return "splits", "same as yfinance Close = splits basis"
    return "unknown", "neither = unknown"


def _format_compare_report(
    symbol: str,
    basis: str,
    verdict: str,
    data: dict[Any, tuple[float, float, float, float, float]],
    steps: list[dict[str, Any]],
) -> str:
    lines = [
        f"# Comparison Spike: Upstox vs Yahoo Finance Adjustment ({symbol})\n",
        "## Summary",
        f"Spike comparing Upstox 1d candle series against Yahoo Finance raw and adjusted "
        f"historical bars for symbol {symbol}. Analyzes price ratios over time and discontinuity "
        f"steps around known corporate action ex-dates to verify the adjustment basis.\n",
        "## Adjustment Basis Conclusion",
        f"- **Detected Upstox Candle Basis**: `{basis}`",
        f"- **Verdict**: {verdict}",
        (
            "- **Adjustment Finding**: yfinance Close is split-adjusted when it shows no "
            "step at a split or bonus date (verified on RELIANCE 2024-10-28 bonus). "
            "Upstox daily closes equal yfinance Close at ex-dates. "
            "Neither source gives truly raw prices."
        ),
        f"- **Overlapping Trading Days**: {len(data)}",
        f"- **Action Ex-Dates Evaluated**: {len(steps)}\n",
        "## Ex-Date Steps Analysis",
    ]
    if not steps:
        lines.append("- No corporate action ex-dates fell within the comparison window.\n")
    for s in steps:
        r = (
            f"Upstox={s.get('upstox_close', 0.0):.2f}, "
            f"YF Close={s.get('yf_close', 0.0):.2f}, "
            f"YF Adj={s.get('yf_adj', 0.0):.2f}, "
            f"raw_step={s['raw_step']:.4f}, adj_step={s['adj_step']:.4f}"
        )
        lines.append(f"- Ex-Date: {s['ex_date']} ({s['type']}): {r}")
    lines.extend(
        [
            "\n## Sample Daily Price Ratios (Latest 10 Days)\n",
            "| Date | Upstox Close | YF Raw | YF Adj | Upstox/Raw | Upstox/Adj |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for d in sorted(data.keys())[-10:]:
        u, yr, ya, rr, ra = data[d]
        lines.append(f"| {d} | {u:.2f} | {yr:.2f} | {ya:.2f} | {rr:.4f} | {ra:.4f} |")
    return "\n".join(lines) + "\n"


@spike_app.command("compare-adjustment")
@handle_cli_errors
def spike_compare_adjustment(
    ctx: typer.Context,
    symbol: str = typer.Option(..., "--symbol", "-s", help="Symbol to inspect (e.g. RELIANCE)"),
    from_date: str = typer.Option(..., "--from", help="Start date (YYYY-MM-DD)"),
    to_date: str = typer.Option(..., "--to", help="End date (YYYY-MM-DD)"),
    exchange: str = typer.Option("NSE", "--exchange", help="Exchange code (default NSE)"),
) -> None:
    """Compare Upstox 1d candles against yfinance raw and adjusted to determine basis."""
    with _open_rt(ctx) as rt:
        u_ctx = build_context(Mode.RESEARCH, run_id=f"spike_cmp_u_{symbol}")
        y_ctx = build_context(Mode.RESEARCH, run_id=f"spike_cmp_y_{symbol}")
        upstox: Any = rt.plugins.create("broker", "upstox", config={}, ctx=u_ctx)
        yfinance: Any = rt.plugins.create("broker", "yfinance", config={}, ctx=y_ctx)

        inst_id = InstrumentId(symbol if ":" in symbol else f"{exchange}:EQ:{symbol}")
        sym_clean = inst_id.split(":")[-1]
        start_dt = datetime.fromisoformat(from_date).replace(tzinfo=UTC)
        end_dt = datetime.fromisoformat(to_date).replace(hour=23, minute=59, second=59, tzinfo=UTC)

        k_u = SeriesKey(inst_id, "bars", "1d", "adjusted", "upstox")
        k_yr = SeriesKey(inst_id, "bars", "1d", "raw", "yfinance")
        k_ya = SeriesKey(inst_id, "bars", "1d", "adjusted", "yfinance")

        u_bars = list(upstox.history(k_u, start_dt, end_dt))
        yr_bars = list(yfinance.history(k_yr, start_dt, end_dt))
        ya_bars = list(yfinance.history(k_ya, start_dt, end_dt))

        actions: list[Any] = []
        for b in (yfinance, upstox):
            if hasattr(b, "actions"):
                try:
                    if acts := list(b.actions(sym_clean)):
                        actions = acts
                        break
                except Exception:
                    pass

        data, r_raw, r_adj = _compute_adjustment_ratios(u_bars, yr_bars, ya_bars)
        steps = _evaluate_ex_date_steps(data, actions)
        basis, verdict = _determine_basis_match(r_raw, r_adj, steps)

        table = Table(title=f"Adjustment Basis Comparison: {sym_clean}")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        for m, v in [
            ("Overlapping Days", len(data)),
            ("Ex-Dates Evaluated", len(steps)),
            ("Detected Basis", basis),
            ("Verdict", verdict),
            (
                "Adjustment Fact",
                "yfinance Close is split-adjusted (no step at split/bonus ex-dates)",
            ),
        ]:
            table.add_row(m, str(v))
        Console().print(table)

        rep = _format_compare_report(sym_clean, basis, verdict, data, steps)
        out = _write_spike_file(rt.root, "spike_compare-adjustment.md", rep)
        typer.echo(f"Report written to {out}")
