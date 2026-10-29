"""Deterministic synthetic market tick data generator.

Generates realistic, reproducible ticks with 5-level order depth, OHLC consistency,
and monotonic timestamps for backtesting, stress testing, and pipeline verification.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pyarrow.parquet as pq

from algodesk.api.market import InstrumentId, Level, Quote, Tick, parse_id
from algodesk.data.manifest import DataManifest, FileRecord, InstrumentRecord
from algodesk.data.schema import events_to_table


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _make_synth_quote(
    instrument: str,
    exch_ts: datetime,
    recv_ts: datetime,
    price: float,
    cum_volume: int,
    rng: random.Random,
) -> Quote:
    bids: list[Level] = []
    asks: list[Level] = []
    for lvl in range(1, 6):
        bids.append(
            Level(
                price=round(price - (lvl * 0.05), 2),
                qty=rng.randint(10, 500),
                orders=rng.randint(1, 5),
            )
        )
        asks.append(
            Level(
                price=round(price + (lvl * 0.05), 2),
                qty=rng.randint(10, 500),
                orders=rng.randint(1, 5),
            )
        )
    return Quote(
        instrument=InstrumentId(instrument),
        exch_ts=exch_ts,
        recv_ts=recv_ts,
        bids=tuple(bids),
        asks=tuple(asks),
        ltp=price,
        volume=cum_volume,
    )


def generate_synthetic_day(
    instrument: str,
    trading_date: date,
    rows: int,
    seed: int,
    pattern: str = "random",
) -> list[Tick | Quote]:
    """Generate deterministic sequence of ticks and quotes for one instrument-day."""
    if pattern not in ("random", "trend"):
        raise ValueError(f"Unknown synthetic data pattern '{pattern}': must be 'random' or 'trend'")
    rng = random.Random(seed)
    parsed = parse_id(instrument)
    sym_hash = int(hashlib.md5(parsed.symbol.encode("utf-8")).hexdigest()[:6], 16)
    base_price = 100.0 + (sym_hash % 2000)

    start_dt = datetime.combine(trading_date, time(3, 45, 0), tzinfo=UTC)
    session_seconds = 6.25 * 3600
    interval_seconds = session_seconds / max(rows, 1)

    events: list[Tick | Quote] = []
    current_price = base_price
    cum_volume = 1000

    for i in range(rows):
        tick_offset = i * interval_seconds + rng.uniform(0.0, min(interval_seconds * 0.5, 0.5))
        exch_ts = start_dt + timedelta(seconds=tick_offset)
        recv_ts = exch_ts + timedelta(milliseconds=int(rng.uniform(1.0, 5.0)))

        if pattern == "trend":
            frac = i / max(rows - 1, 1)
            osc = math.sin(2.0 * math.pi * 3.5 * frac)
            raw_p = base_price * (1.0 + 0.05 * osc)
            current_price = max(1.0, round(round(raw_p / 0.05) * 0.05, 2))
        else:
            delta_ticks = rng.choice([-2, -1, -1, 0, 0, 1, 1, 2])
            current_price = max(1.0, round(current_price + (delta_ticks * 0.05), 2))

        step_qty = rng.choice([1, 5, 10, 25, 50, 100])
        cum_volume += step_qty

        if i % 4 == 0:
            events.append(
                _make_synth_quote(instrument, exch_ts, recv_ts, current_price, cum_volume, rng)
            )
        else:
            events.append(
                Tick(
                    instrument=InstrumentId(instrument),
                    exch_ts=exch_ts,
                    recv_ts=recv_ts,
                    ltp=current_price,
                    ltq=step_qty,
                    volume=cum_volume,
                    oi=None,
                )
            )

    return events


def synth_ticks(
    symbols: Sequence[str],
    days: int,
    rows_per_day: int,
    seed: int,
    data_dir: Path | str,
    manifest: DataManifest,
    pattern: str = "random",
) -> int:
    """Generate and write deterministic synthetic market data across symbols and days."""
    d_dir = Path(data_dir)
    ref_date = date(2026, 10, 26)
    total_written = 0

    # Ensure demo instruments are registered in instruments table
    inst_records = []
    for sym in symbols:
        p = parse_id(sym)
        inst_records.append(
            InstrumentRecord(
                id=sym,
                exchange=p.exchange,
                segment=p.segment,
                trading_symbol=p.symbol,
                kind="equity" if p.segment == "EQ" else "derivative",
                underlying=p.symbol,
            )
        )
    manifest.record_instruments(inst_records)

    trading_days: list[date] = []
    curr = ref_date
    while len(trading_days) < days:
        if curr.weekday() < 5:
            trading_days.append(curr)
        curr -= timedelta(days=1)

    for sym_idx, sym in enumerate(symbols):
        parsed = parse_id(sym)
        for day_offset, trading_d in enumerate(trading_days):
            day_seed = seed + (sym_idx * 1000) + (day_offset * 10)

            events = generate_synthetic_day(
                instrument=sym,
                trading_date=trading_d,
                rows=rows_per_day,
                seed=day_seed,
                pattern=pattern,
            )

            d_str = trading_d.isoformat()
            pdir = (
                d_dir
                / "ticks"
                / f"exchange={parsed.exchange}"
                / f"segment={parsed.segment}"
                / f"symbol={parsed.symbol}"
                / f"date={d_str}"
            )
            pdir.mkdir(parents=True, exist_ok=True)
            day_file = pdir / "day.parquet"

            table = events_to_table(events)
            pq.write_table(table, day_file, compression="zstd")

            rec = FileRecord(
                path=day_file.as_posix(),
                instrument=sym,
                kind="ticks",
                timeframe="tick",
                date=d_str,
                rows=len(events),
                bytes=day_file.stat().st_size,
                first_ts=events[0].exch_ts.isoformat(),
                last_ts=events[-1].exch_ts.isoformat(),
                sha256=_compute_sha256(day_file),
                source="synthetic",
                state="day",
                created_ts=datetime.now(UTC).isoformat(),
            )
            manifest.record_file(rec)
            total_written += len(events)

    return total_written
