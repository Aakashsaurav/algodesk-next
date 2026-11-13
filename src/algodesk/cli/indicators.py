"""Technical indicators inspection and computation CLI commands."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import typer

from algodesk.api.errors import DataError
from algodesk.api.kinds import Mode
from algodesk.api.market import InstrumentId, SeriesKey
from algodesk.api.protocols import Indicator
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.cli.data import _build_data_service, _parse_data_cli_dt
from algodesk.ops.context import build_context

_IST = ZoneInfo("Asia/Kolkata")

indicators_app = typer.Typer(
    name="indicators",
    help="Inspect, list, and compute technical indicators over market data.",
)


def _collect_indicator_specs(rt: Any) -> list[Any]:
    p_ctx = build_context(Mode.RESEARCH, run_id="indicators_list")
    specs: list[Any] = []
    for p_info in rt.plugins.list("indicator"):
        plugin = rt.plugins.create("indicator", p_info.name, config={}, ctx=p_ctx)
        if hasattr(plugin, "get_indicator"):
            caps = getattr(p_info.manifest, "capabilities", {})
            for cap_name, enabled in caps.items():
                if enabled:
                    try:
                        ind = plugin.get_indicator(cap_name)
                        if hasattr(ind, "spec"):
                            specs.append(ind.spec)
                    except Exception:
                        pass
        elif hasattr(plugin, "spec"):
            specs.append(plugin.spec)
    return specs


@indicators_app.command("list")
@handle_cli_errors
def indicators_list(ctx: typer.Context) -> None:
    """List available technical indicators with inputs, outputs, and parameters."""
    with _open_rt(ctx, ensure=False) as rt:
        specs = _collect_indicator_specs(rt)

    if not specs:
        typer.echo("No indicators found.")
        return

    headers = ["NAME", "INPUTS", "OUTPUTS", "DEFAULTS", "WARM-UP", "TIMEFRAMES"]
    rows: list[list[str]] = []
    for s in sorted(specs, key=lambda x: str(x.name)):
        defaults_str = (
            ", ".join(f"{k}={v}" for k, v in s.params_schema.items()) if s.params_schema else "-"
        )
        tf_str = ", ".join(s.timeframes) if s.timeframes else "any"
        rows.append(
            [
                str(s.name),
                ", ".join(s.inputs),
                ", ".join(s.outputs),
                defaults_str,
                str(s.warmup),
                tf_str,
            ]
        )
    typer.echo(format_table(headers, rows))


@indicators_app.command("show")
@handle_cli_errors
def indicators_show(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Indicator name (e.g. sma, macd, atr)"),
) -> None:
    """Show detailed specification for an indicator."""
    with _open_rt(ctx, ensure=False) as rt:
        specs = _collect_indicator_specs(rt)

    target = name.lower()
    match = next((s for s in specs if str(s.name).lower() == target), None)
    if match is None:
        typer.echo(
            f"Indicator '{name}' not found. "
            "Run 'algodesk indicators list' to see available indicators."
        )
        raise typer.Exit(code=1)

    defaults_str = (
        ", ".join(f"{k}={v}" for k, v in match.params_schema.items())
        if match.params_schema
        else "none"
    )
    tf_str = ", ".join(match.timeframes) if match.timeframes else "any"

    typer.echo(f"Indicator:   {match.name}")
    typer.echo(f"Inputs:      {', '.join(match.inputs)}")
    typer.echo(f"Outputs:     {', '.join(match.outputs)}")
    typer.echo(f"Defaults:    {defaults_str}")
    typer.echo(f"Warm-up:     {match.warmup} bars")
    typer.echo(f"Timeframes:  {tf_str}")


def _parse_cli_params(params: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for p in params:
        if "=" not in p:
            raise typer.BadParameter(
                f"Parameter must be in key=value format, got '{p}'",
                param_hint="--param",
            )
        k, v = p.split("=", 1)
        k, v = k.strip(), v.strip()
        try:
            val: Any = int(v)
        except ValueError:
            try:
                val = float(v)
            except ValueError:
                val = v
        parsed[k] = val
    return parsed


def _format_bar_timestamp(ts: datetime, is_daily: bool) -> str:
    local_ts = ts.astimezone(_IST)
    return local_ts.strftime("%Y-%m-%d") if is_daily else local_ts.strftime("%Y-%m-%d %H:%M:%S")


def _build_indicator_inputs(spec: Any, bars: list[Any]) -> Any:
    if len(spec.inputs) == 1:
        inp_name = spec.inputs[0]
        if inp_name == "close":
            return [b.close for b in bars]
        if inp_name == "volume":
            return [float(b.volume) for b in bars]
    return {
        "high": [b.high for b in bars],
        "low": [b.low for b in bars],
        "close": [b.close for b in bars],
        "volume": [float(b.volume) for b in bars],
        "open": [b.open for b in bars],
        "ts": [b.start_ts for b in bars],
    }


def _render_compute_table(
    bars: list[Any],
    output_data: Any,
    spec: Any,
    is_daily: bool,
) -> str:
    date_header = "DATE" if is_daily else "DATE/TIME (IST)"
    out_headers = [o.upper() for o in spec.outputs]
    headers = [date_header, "CLOSE", *out_headers]

    disp_bars = bars[-10:]
    n_disp = len(disp_bars)
    start_idx = len(bars) - n_disp

    rows: list[list[str]] = []
    for i in range(start_idx, len(bars)):
        b = bars[i]
        date_val = _format_bar_timestamp(b.start_ts, is_daily)
        row = [date_val, f"{b.close:.2f}"]
        for out_name in spec.outputs:
            if isinstance(output_data, Mapping):
                val = output_data.get(out_name, [])[i] if out_name in output_data else None
            else:
                val = output_data[i]
            val_str = f"{val:.4f}" if val is not None else "-"
            row.append(val_str)
        rows.append(row)
    return format_table(headers, rows)


@indicators_app.command("compute")
@handle_cli_errors
def indicators_compute(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Indicator name (e.g. sma, atr, bollinger, macd)"),
    symbol: str = typer.Argument(..., help="Symbol (e.g. RELIANCE, TCS)"),
    timeframe: str = typer.Argument(..., help="Timeframe (e.g. 1d, 5m)"),
    from_d: str = typer.Option(..., "--from", help="Start date/time (YYYY-MM-DD)"),
    to_d: str = typer.Option(..., "--to", help="End date/time (YYYY-MM-DD)"),
    param: list[str] = typer.Option([], "--param", "-p", help="Indicator parameter key=value"),
    source: str = typer.Option("upstox", "--source", "-s", help="Data source broker plugin"),
    accept_unverified: bool = typer.Option(
        False, "--accept-unverified", help="Accept unverified corporate actions"
    ),
) -> None:
    """Compute indicator values over market data and print the last 10 rows."""
    start_dt = _parse_data_cli_dt(from_d, is_end=False)
    end_dt = _parse_data_cli_dt(to_d, is_end=True)
    inst_id = InstrumentId(symbol if ":" in symbol else f"NSE:EQ:{symbol}")
    key = SeriesKey(
        instrument=inst_id,
        kind="bars",
        timeframe=timeframe,
        adjustment="adjusted",
        source=source,
    )
    parsed_params = _parse_cli_params(param)

    with _open_rt(ctx, ensure=False) as rt:
        ds = _build_data_service(rt, source)
        try:
            data_slice = ds.get(
                series=key,
                start=start_dt,
                end=end_dt,
                download="adjusted",
                adjust="total",
                accept_unverified=accept_unverified,
            )
        except Exception as exc:
            fetch_cmd = f"algodesk data get {symbol} {timeframe} {from_d} {to_d} --source {source}"
            raise DataError(
                what=f"Failed to fetch data for {symbol} ({timeframe}): {exc}",
                where="algodesk indicators compute",
                fix=f"Run: {fetch_cmd}",
            ) from exc

        bars = [b for b in data_slice.bars if getattr(b, "confirmed", True)]
        if not bars:
            fetch_cmd = f"algodesk data get {symbol} {timeframe} {from_d} {to_d} --source {source}"
            what = f"No completed bars found for {symbol} ({timeframe}) between {from_d} and {to_d}"
            raise DataError(
                what=what,
                where="algodesk indicators compute",
                fix=f"Fetch data first using:\n  {fetch_cmd}",
            )

        p_ctx = build_context(Mode.RESEARCH, run_id=f"ind_compute_{name}")
        ind_plugin = cast(
            Indicator,
            rt.plugins.create(
                "indicator",
                "builtin",
                config={"indicator": name, **parsed_params},
                ctx=p_ctx,
            ),
        )
        spec = ind_plugin.spec
        inputs = _build_indicator_inputs(spec, bars)
        output_data = ind_plugin.compute(inputs)

    is_daily = timeframe.lower() in ("1d", "daily")
    table_str = _render_compute_table(bars, output_data, spec, is_daily)
    typer.echo(table_str)
