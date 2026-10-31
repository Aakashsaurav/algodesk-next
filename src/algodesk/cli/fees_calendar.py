"""Fee calculations and trading calendar CLI commands."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import typer

from algodesk.api.kinds import Mode
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.ops.context import build_context

calendar_app = typer.Typer(help="Inspect trading calendars, sessions, and market holidays.")
fees_app = typer.Typer(help="Calculate trade charges and inspect fee schedule vectors.")


@calendar_app.command("show")
@handle_cli_errors
def calendar_show(
    ctx: typer.Context,
    exchange: str = typer.Option(
        ..., "--exchange", "-e", help="Exchange code (e.g. nse, bse, mcx)"
    ),
    segment: str = typer.Option(
        "", "--segment", "-s", help="Segment code (e.g. eq, com, fno, cds)"
    ),
    date_str: str = typer.Option(..., "--date", "-d", help="Date in YYYY-MM-DD format"),
) -> None:
    """Show trading sessions for an exchange and date."""
    d = date.fromisoformat(date_str)
    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"cal_show_{exchange}")
        cal: Any = rt.plugins.create(
            "calendar", "exchange", config={"exchange": exchange}, ctx=p_ctx
        )
        seg = segment or ("com" if exchange.lower() == "mcx" else "eq")
        sessions = cal.sessions(d, segment=seg)
        if not sessions:
            typer.echo(f"Market closed on {d} for {exchange.upper()}:{seg}")
            return
        headers = ["SESSION", "OPEN (UTC)", "CLOSE (UTC)", "OPEN (IST)", "CLOSE (IST)"]
        ist_tz = ZoneInfo("Asia/Kolkata")
        rows: list[list[str]] = []
        for s in sessions:
            open_ist = s.start.astimezone(ist_tz).strftime("%H:%M")
            close_ist = s.end.astimezone(ist_tz).strftime("%H:%M")
            open_utc = s.start.strftime("%H:%M")
            close_utc = s.end.strftime("%H:%M")
            rows.append([s.name, open_utc, close_utc, open_ist, close_ist])
        typer.echo(format_table(headers, rows))


@calendar_app.command("holidays")
@handle_cli_errors
def calendar_holidays(
    ctx: typer.Context,
    exchange: str = typer.Option(
        ..., "--exchange", "-e", help="Exchange code (e.g. nse, bse, mcx)"
    ),
    year: int | None = typer.Option(None, "--year", "-y", help="Filter holidays by year"),
) -> None:
    """List declared market holidays for an exchange."""
    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"cal_holidays_{exchange}")
        cal: Any = rt.plugins.create(
            "calendar", "exchange", config={"exchange": exchange}, ctx=p_ctx
        )
        h_list = cal.holidays(year=year)
        if not h_list:
            typer.echo(
                f"No holidays found for {exchange.upper()}" + (f" in {year}" if year else "")
            )
            return
        headers = ["DATE", "NAME", "CLOSED"]
        rows: list[list[str]] = []
        for h in h_list:
            closed_str = ", ".join(h.get("closed", ["all"]))
            rows.append([str(h["date"]), h["name"], closed_str])
        typer.echo(format_table(headers, rows))
        typer.echo(f"\n{len(h_list)} holiday(s) found for {exchange.upper()}")


@calendar_app.command("check")
@handle_cli_errors
def calendar_check(
    ctx: typer.Context,
    exchange: str = typer.Option(
        ..., "--exchange", "-e", help="Exchange code (e.g. nse, bse, mcx)"
    ),
    days: int = typer.Option(
        ..., "--days", "-d", help="Days forward to check coverage and sessions"
    ),
) -> None:
    """Verify calendar coverage and pending special sessions for the next N days."""
    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"cal_check_{exchange}")
        cal: Any = rt.plugins.create(
            "calendar", "exchange", config={"exchange": exchange}, ctx=p_ctx
        )
        cov_start, cov_end = cal.coverage()
        today = date.today()
        target = today + timedelta(days=days)

        problems: list[str] = []
        if today < cov_start or target > cov_end:
            problems.append(
                f"Coverage [{cov_start}, {cov_end}] does not span required [{today}, {target}]"
            )

        for sp in cal.pending_special_sessions():
            sp_date = sp.get("date")
            if sp_date and today <= sp_date <= target:
                problems.append(
                    f"Pending special session without timings on {sp_date}: {sp.get('name')}"
                )

        if problems:
            typer.echo(f"Calendar check failed for {exchange.upper()}:")
            for p in problems:
                typer.echo(f"  - {p}")
            raise typer.Exit(code=1)

        typer.echo(
            f"OK: Calendar for {exchange.upper()} covers next {days} days "
            "with no pending special sessions."
        )


@fees_app.command("calc")
@handle_cli_errors
def fees_calc_cmd(
    ctx: typer.Context,
    exchange: str = typer.Option("nse", "--exchange", "-e", help="Exchange code (e.g. nse)"),
    segment: str = typer.Option("eq", "--segment", "-s", help="Segment code (e.g. eq, fno)"),
    product: str = typer.Option(
        "MIS", "--product", "-p", help="Product type (e.g. CNC, MIS, NRML)"
    ),
    side: str = typer.Option("buy", "--side", help="Order side (buy, sell)"),
    qty: int = typer.Option(..., "--qty", "-q", help="Execution quantity"),
    price: float = typer.Option(..., "--price", help="Execution unit price"),
    date_str: str = typer.Option(..., "--date", "-d", help="Date in YYYY-MM-DD format"),
    symbol: str | None = typer.Option(None, "--symbol", help="Optional scrip symbol"),
) -> None:
    """Calculate itemised trade charges and total for a given trade."""
    from algodesk.fees.engine import FeeEngine

    d = date.fromisoformat(date_str)
    price_dec = Decimal(str(price))
    turnover = Decimal(qty) * price_dec

    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id=f"fees_calc_{exchange}")
        plugin_name = f"zerodha_{exchange.lower()}"
        fees_plugin: Any = rt.plugins.create("fees", plugin_name, config={}, ctx=p_ctx)
        sch = fees_plugin.schedule()
        charges = FeeEngine.calculate_trade_charges(
            turnover=turnover,
            side=side,
            segment=segment,
            product=product,
            trade_date=d,
            schedule=sch,
        )

    headers = ["COMPONENT", "AMOUNT (INR)"]
    rows = [[k, f"{v:.2f}"] for k, v in charges.items.items()]
    rows.append(["TOTAL", f"{charges.total:.2f}"])
    typer.echo(
        f"Itemised charges for {exchange.upper()}:{segment.upper()}:{product.upper()} "
        f"{side.upper()} {qty} @ {price:.2f} (Turnover: {turnover:.2f}) on {d}:"
    )
    typer.echo(format_table(headers, rows))


@fees_app.command("vectors")
@handle_cli_errors
def fees_vectors_cmd(ctx: typer.Context) -> None:
    """Display the verified trade fee vectors table."""
    from algodesk.fees.engine import FeeEngine

    vectors: list[tuple[str, str, str, str, int, Decimal, bool]] = [
        ("1", "eq", "CNC", "buy", 100, Decimal("500.00"), True),
        ("2", "eq", "CNC", "sell", 100, Decimal("500.00"), True),
        ("3", "eq", "CNC", "sell", 50, Decimal("500.00"), True),
        ("4", "eq", "MIS", "buy", 10, Decimal("2500.00"), True),
        ("5", "eq", "MIS", "sell", 10, Decimal("2500.00"), True),
        ("6", "eq", "MIS", "buy", 1000, Decimal("500.00"), True),
        ("7", "fno", "NRML", "buy", 250, Decimal("2000.00"), True),
        ("8", "fno", "NRML", "sell", 250, Decimal("2000.00"), True),
    ]

    with _open_rt(ctx, ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id="fees_vectors")
        fees_plugin: Any = rt.plugins.create("fees", "zerodha_nse", config={}, ctx=p_ctx)
        sch = fees_plugin.schedule()

        headers = ["#", "SEGMENT", "PRODUCT", "SIDE", "QTY", "PRICE", "TURNOVER", "TOTAL", "STATUS"]
        rows: list[list[str]] = []
        d = date(2026, 3, 26)

        for num, seg, prod, s, q, p, is_first in vectors:
            turnover = Decimal(q) * p
            charges = FeeEngine.calculate_trade_charges(
                turnover=turnover,
                side=s,
                segment=seg,
                product=prod,
                trade_date=d,
                schedule=sch,
                is_first_scrip_day=is_first,
            )
            rows.append(
                [
                    num,
                    seg.upper(),
                    prod.upper(),
                    s.upper(),
                    str(q),
                    f"{p:.2f}",
                    f"{turnover:.2f}",
                    f"{charges.total:.2f}",
                    "computed",
                ]
            )

    typer.echo("Computed fee vectors (pending your hand check against Zerodha's calculator):")
    typer.echo(format_table(headers, rows))
