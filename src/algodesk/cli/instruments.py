"""Instrument inspection, master diff, and trading feasibility CLI commands."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer

from algodesk.api.errors import ConfigError
from algodesk.api.market import InstrumentId
from algodesk.cli.common import _open_rt, format_table, handle_cli_errors
from algodesk.data.aliases import resolve_alias
from algodesk.data.groups import get_group, list_group_details
from algodesk.data.instruments_store import InstrumentStore

instruments_app = typer.Typer(help="Inspect, diff, and verify tradable instruments.")


@instruments_app.command("diff")
@handle_cli_errors
def instruments_diff_cmd(
    ctx: typer.Context,
    date_a: str | None = typer.Argument(None, help="Base snapshot date YYYY-MM-DD"),
    date_b: str | None = typer.Argument(None, help="Target snapshot date YYYY-MM-DD"),
    source: str = typer.Option("upstox", "--source", "-s", help="Broker source name"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Compare daily instrument master snapshots.

    Reports added, removed, and changed instruments.
    """
    with _open_rt(ctx, ensure=False) as rt:
        store = InstrumentStore(rt.paths.data)
        dates = store.list_dates(source)

        # Also check local cache for seeding if empty
        if not dates:
            cache_p = Path(".local/cache/instruments")
            if cache_p.is_dir():
                for cp in sorted(cache_p.glob("upstox_master_*.json.gz")):
                    d_str = cp.stem.replace("upstox_master_", "").replace(".json", "")
                    try:
                        dt = date.fromisoformat(d_str)
                        store.save_master(source, dt, cp.read_bytes())
                    except Exception:
                        pass
                dates = store.list_dates(source)

        if not dates:
            typer.echo(f"No instrument master snapshots found for {source}.")
            return

        dt_b: date
        dt_a: date
        if date_b is not None:
            dt_b = date.fromisoformat(date_b)
        else:
            dt_b = dates[-1]

        if date_a is not None:
            dt_a = date.fromisoformat(date_a)
        elif len(dates) >= 2:
            dt_a = dates[-2]
        else:
            dt_a = dt_b

        report = store.diff(source, dt_a, dt_b, journal=getattr(rt, "journal", None))
        if json_out:
            typer.echo(
                json.dumps(
                    {
                        "source": source,
                        "date_a": dt_a.isoformat(),
                        "date_b": dt_b.isoformat(),
                        "added": [str(i.id) for i in report.added],
                        "removed": [str(i.id) for i in report.removed],
                        "changed": [
                            {
                                "id": str(c.id),
                                "changes": {
                                    k: [str(v[0]), str(v[1])] for k, v in c.changes.items()
                                },
                            }
                            for c in report.changed
                        ],
                    },
                    indent=2,
                )
            )
            return

        typer.echo(report.summary())
        if report.added:
            typer.echo(f"\nAdded ({len(report.added)}):")
            for inst in report.added[:20]:
                typer.echo(f"  + {inst.id}")
            if len(report.added) > 20:
                typer.echo(f"  ... and {len(report.added) - 20} more")

        if report.removed:
            typer.echo(f"\nRemoved ({len(report.removed)}):")
            for inst in report.removed[:20]:
                typer.echo(f"  - {inst.id}")
            if len(report.removed) > 20:
                typer.echo(f"  ... and {len(report.removed) - 20} more")

        if report.changed:
            typer.echo(f"\nChanged ({len(report.changed)}):")
            for c in report.changed[:20]:
                diff_strs = [f"{k}: {v[0]} -> {v[1]}" for k, v in c.changes.items()]
                typer.echo(f"  ~ {c.id}: {', '.join(diff_strs)}")
            if len(report.changed) > 20:
                typer.echo(f"  ... and {len(report.changed) - 20} more")


def _find_latest_close_price(dm: Any, data_dir: Path, instrument: InstrumentId) -> Decimal | None:
    """Find the latest recorded close/LTP price for an instrument from catalog parts or ticks."""
    try:
        import pyarrow.parquet as pq

        from algodesk.data.aliases import resolve_alias

        canonical_str = str(resolve_alias(instrument))
        latest_ts: str = ""
        latest_px: Decimal | None = None

        # 1. Check parts (bars)
        for p in dm.get_parts():
            if canonical_str in p.dataset_id or str(instrument) in p.dataset_id:
                p_path = Path(p.path)
                if not p_path.is_absolute():
                    p_path = data_dir / p_path
                if p_path.is_file():
                    tbl = pq.read_table(p_path)
                    if "close" in tbl.column_names and len(tbl) > 0:
                        ts = str(p.last_ts or "")
                        if ts >= latest_ts:
                            latest_ts = ts
                            latest_px = Decimal(str(tbl["close"][-1].as_py()))

        # 2. Check files (ticks)
        for f in dm.get_files():
            if str(resolve_alias(f.instrument)) == canonical_str:
                f_path = Path(f.path)
                if not f_path.is_absolute():
                    f_path = data_dir / f_path
                if f_path.is_file():
                    tbl = pq.read_table(f_path)
                    for col in ("close", "ltp", "price"):
                        if col in tbl.column_names and len(tbl) > 0:
                            ts = str(f.last_ts or f.date or "")
                            if ts >= latest_ts:
                                latest_ts = ts
                                latest_px = Decimal(str(tbl[col][-1].as_py()))
                            break

        return latest_px
    except Exception:
        return None


@instruments_app.command("feasibility")
@handle_cli_errors
def instruments_feasibility_cmd(
    ctx: typer.Context,
    group: str = typer.Option(..., "--group", "-g", help="Configured instrument group name"),
    capital: float = typer.Option(..., "--capital", "-c", help="Allocated capital in INR"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Evaluate trading feasibility and lot capital requirements for an instrument group."""
    with _open_rt(ctx, ensure=False) as rt:
        groups_dir = rt.root / "config" / "groups"
        members = get_group(group, groups_dir)
        if members is None:
            details = list_group_details(groups_dir)
            raise ConfigError(
                what=f"Instrument group '{group}' not found",
                where=f"config/groups/{group}.toml",
                fix=f"Available groups: {', '.join(details.keys())}",
            )

        cap_dec = Decimal(str(capital))
        rows: list[list[str]] = []
        json_rows: list[dict[str, Any]] = []

        from algodesk.api.kinds import Mode, PluginKind
        from algodesk.ops.context import build_context

        broker = None
        try:
            p_ctx = build_context(Mode.RESEARCH, run_id="instruments_feasibility")
            broker = rt.plugins.create(PluginKind.BROKER, "upstox", config={}, ctx=p_ctx)
            if hasattr(broker, "instruments"):
                # Ensure loaded
                _ = broker.instruments()
        except Exception:
            pass

        from algodesk.data.manifest import DataManifest

        m_path = rt.paths.data / "state" / "data_manifest.db"
        if not m_path.exists() and (rt.paths.data / "manifest.db").exists():
            m_path = rt.paths.data / "manifest.db"
        dm = DataManifest(m_path)

        for raw_sym in members:
            canonical_id = resolve_alias(raw_sym)
            lot_size = 1
            if broker and hasattr(broker, "_instrument_manager"):
                try:
                    inst = broker._instrument_manager.resolve(canonical_id)
                    lot_size = inst.lot_size
                except Exception:
                    pass

            close_px = _find_latest_close_price(dm, rt.paths.data, canonical_id)
            if close_px is not None:
                lot_value = Decimal(str(lot_size)) * close_px
                fits = "YES" if lot_value <= cap_dec else "NO"
                px_str = f"₹{close_px:,.2f}"
                val_str = f"₹{lot_value:,.2f}"
            else:
                lot_value = None
                fits = "UNKNOWN"
                px_str = "no price"
                val_str = "no price"

            rows.append([str(canonical_id), str(lot_size), px_str, val_str, fits])
            json_rows.append(
                {
                    "instrument": str(canonical_id),
                    "lot_size": lot_size,
                    "close_price": float(close_px) if close_px else None,
                    "lot_value": float(lot_value) if lot_value else None,
                    "fits": fits,
                }
            )

        if json_out:
            typer.echo(
                json.dumps(
                    {
                        "group": group,
                        "capital": float(capital),
                        "instruments": json_rows,
                        "margin_estimated": False,
                    },
                    indent=2,
                )
            )
            return

        headers = [
            "INSTRUMENT",
            "LOT SIZE",
            "CLOSE PRICE",
            "LOT VALUE",
            f"FITS (CAPITAL: ₹{capital:,.0f})",
        ]
        typer.echo(format_table(headers, rows))
        typer.echo(
            "\nNote: Margin is not estimated; calculation is based on full lot notional value."
        )
