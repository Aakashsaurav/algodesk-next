"""Summary: Replay proof test for M1-15.

Tests that a fake-feed paper session with:
1. Quote-only sequence numbers at market close
2. A late bar close
3. An open position at stop
4. An unfilled limit order cancelled at market close

replays deterministically and produces identical signals, orders, fills,
metrics, open positions at stop, and equity points with compare_runs returning equal True.
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api import (
    InstrumentId,
    Level,
    Order,
    OrderSpec,
    OrderState,
    OrderType,
    Product,
    Quote,
    Side,
    Tick,
    Validity,
)
from algodesk.cli.app import app
from algodesk.engine.clock import SimClock
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime
from algodesk.results.compare import compare_runs

IST = ZoneInfo("Asia/Kolkata")


def _seed_cache(proj: Path) -> None:
    real_cache = (
        Path(__file__).resolve().parent.parent.parent.parent / ".local" / "cache" / "instruments"
    )
    today = datetime.now(IST).date()
    target = proj / ".local" / "cache" / "instruments"
    target.mkdir(parents=True, exist_ok=True)
    if real_cache.is_dir():
        for f in real_cache.glob("upstox_master_*.json.gz"):
            shutil.copyfile(f, target / f"upstox_master_{today.isoformat()}.json.gz")
            break


def test_fake_feed_replay_quote_seqs_late_bar_open_position_unfilled_order(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance test (M1-15 Item 12):

    Fake-feed session with quote-only sequence numbers at market close,
    a late bar close, an open position at stop, and an unfilled order.
    Paper and replay must be equal in signals, orders, fills, metrics, and equity points.
    """
    _seed_cache(tmp_project)

    # Configure demo_paper_fast for test parameters so paper and replay share identical config
    cfg_file = tmp_project / "config" / "runs" / "demo_paper_fast.toml"
    cfg_text = cfg_file.read_text(encoding="utf-8")
    old_params = (
        'params = { timeframe = "1m", fast = 3, slow = 8, qty = 1, '
        'exit_time = "15:15", product = "MIS" }'
    )
    new_params = (
        'params = { timeframe = "1m", fast = 2, slow = 4, qty = 1, '
        'exit_time = "15:45", product = "MIS" }'
    )
    cfg_text = cfg_text.replace(old_params, new_params)
    cfg_file.write_text(cfg_text, encoding="utf-8")

    inst = InstrumentId("NSE:EQ:RELIANCE")
    t0 = datetime(2026, 11, 4, 9, 40, 0, tzinfo=UTC)  # 15:10:00 IST

    events: list[Tick | Quote] = []

    # Fast = 2, Slow = 4.
    # Bars 1 to 5: prices steady at 2500.0 (warm up SMA windows)
    prices_by_minute = [
        2500.0,  # 15:10 (Bar 1)
        2500.0,  # 15:11 (Bar 2)
        2500.0,  # 15:12 (Bar 3)
        2500.0,  # 15:13 (Bar 4)
        2500.0,  # 15:14 (Bar 5)
        2490.0,  # 15:15 (Bar 6: dip below slow SMA -> fast < slow)
        2480.0,  # 15:16 (Bar 7: continued dip -> fast < slow)
        2520.0,  # 15:17 (Bar 8: sharp rally -> fast crosses above slow!)
    ]

    vol = 100
    for minute_idx, price in enumerate(prices_by_minute):
        t_base = t0 + timedelta(minutes=minute_idx)
        for s in (10, 30, 50):
            t_tick = t_base + timedelta(seconds=s)
            vol += 50
            events.append(
                Tick(
                    instrument=inst,
                    exch_ts=t_tick,
                    recv_ts=t_tick + timedelta(milliseconds=10),
                    ltp=price,
                    ltq=1,
                    volume=vol,
                )
            )

    # Bar 9 tick at 15:18:10 (closes Bar 8, evaluates crossover -> emits BUY 1 RELIANCE)
    t_b9 = t0 + timedelta(minutes=8, seconds=10)  # 15:18:10 IST
    vol += 50
    events.append(
        Tick(
            instrument=inst,
            exch_ts=t_b9,
            recv_ts=t_b9 + timedelta(milliseconds=10),
            ltp=2520.0,
            ltq=1,
            volume=vol,
        )
    )

    # 2. Late bar close: Bar 9 started at 15:18:00, nominal end 15:19:00.
    # No ticks arrive for 5 minutes. Next tick arrives at 15:24:00 IST.
    # exch_ts (15:24:00) >= active bar end (15:19:00) -> closes bar late.
    t_late = t0 + timedelta(minutes=14, seconds=0)  # 15:24:00 IST
    vol += 50
    events.append(
        Tick(
            instrument=inst,
            exch_ts=t_late,
            recv_ts=t_late + timedelta(milliseconds=10),
            ltp=2520.0,
            ltq=1,
            volume=vol,
        )
    )

    # Subsequent ticks up to 15:28:00 (prices steady; no sell signal; position remains open)
    for m in (16, 18):
        t_tick = t0 + timedelta(minutes=m)
        vol += 50
        events.append(
            Tick(
                instrument=inst,
                exch_ts=t_tick,
                recv_ts=t_tick + timedelta(milliseconds=10),
                ltp=2520.0,
                ltq=1,
                volume=vol,
            )
        )

    # 3. Quote-only sequence numbers at market close (15:30:00 IST = 10:00:00 UTC)
    # Ticks cease; Quotes arrive before, at, and after 15:30:00 IST.
    t_q1 = t0 + timedelta(minutes=19, seconds=50)  # 15:29:50 IST
    events.append(
        Quote(
            instrument=inst,
            exch_ts=t_q1,
            recv_ts=t_q1 + timedelta(milliseconds=10),
            bids=(Level(price=2519.0, qty=50, orders=1),),
            asks=(Level(price=2521.0, qty=50, orders=1),),
            ltp=2520.0,
            volume=vol,
        )
    )

    t_q2 = t0 + timedelta(minutes=20, seconds=0)  # 15:30:00 IST (exact market close)
    events.append(
        Quote(
            instrument=inst,
            exch_ts=t_q2,
            recv_ts=t_q2 + timedelta(milliseconds=10),
            bids=(Level(price=2519.0, qty=50, orders=1),),
            asks=(Level(price=2521.0, qty=50, orders=1),),
            ltp=2520.0,
            volume=vol,
        )
    )

    t_q3 = t0 + timedelta(minutes=20, seconds=10)  # 15:30:10 IST
    events.append(
        Quote(
            instrument=inst,
            exch_ts=t_q3,
            recv_ts=t_q3 + timedelta(milliseconds=10),
            bids=(Level(price=2519.0, qty=50, orders=1),),
            asks=(Level(price=2521.0, qty=50, orders=1),),
            ltp=2520.0,
            volume=vol,
        )
    )

    # 1. Run paper session
    paper_dir: Path
    with open_runtime(tmp_project, run="demo_paper_fast", ensure=True) as rt:
        clock = SimClock(t0)
        runner = SessionRunner(rt, mode="paper", clock=clock, dry_run=False)

        async def _feed_stream(c: dict[str, Any], clk: SimClock):
            # Inject an unfillable limit order into OMS (cancelled at market close 15:30)
            oms = c["oms"]
            unfillable = Order(
                client_ref="unfillable-limit-1",
                spec=OrderSpec(
                    instrument=inst,
                    side=Side.BUY,
                    qty=1,
                    type=OrderType.LIMIT,
                    price=Decimal("10.0"),  # Far below LTP -> rests unfilled
                    product=Product.MIS,
                    validity=Validity.DAY,
                ),
                state=OrderState.ACKNOWLEDGED,
                created_ts=t0,
                filled_qty=0,
                avg_price=Decimal("0.0"),
                venue_ref="V-UNFILLABLE",
            )
            oms._orders[unfillable.client_ref] = unfillable

            for ev in events:
                clk.set_time(ev.exch_ts)
                yield ev
                await asyncio.sleep(0.001)

            # Advance clock past stop_time (15:35:00 IST) to trigger clean session stop
            clk.set_time(t0 + timedelta(minutes=25, seconds=5))  # 15:35:05 IST
            c["scheduler"].advance_to(clk.now())
            c["scheduler"].fire_due()

        monkeypatch.setattr(runner, "_build_event_stream", _feed_stream)
        paper_dir = asyncio.run(runner.run())

    assert paper_dir.is_dir()
    p_metrics = json.loads((paper_dir / "metrics.json").read_text(encoding="utf-8"))
    assert p_metrics["stop_reason"] == "end of session"

    # Verify open position at stop
    p_open_positions = p_metrics.get("open_positions", [])
    assert len(p_open_positions) == 1
    assert p_open_positions[0]["instrument"] == "NSE:EQ:RELIANCE"
    assert p_open_positions[0]["qty"] == 1

    # Verify SUMMARY.md lists Open at stop
    p_summary = (paper_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Open at stop: NSE:EQ:RELIANCE (qty=1)" in p_summary

    # Verify the unfillable order was cancelled at market close
    p_events = [
        json.loads(line)
        for line in (paper_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    def _payload(ev: dict[str, Any]) -> dict[str, Any]:
        p = ev.get("payload_json", {})
        if isinstance(p, str):
            try:
                return json.loads(p)
            except Exception:
                return {}
        return p if isinstance(p, dict) else {}

    p_cancels = [
        _payload(e)
        for e in p_events
        if e.get("kind") == "order.event"
        and _payload(e).get("to_state") in ("CANCEL_REQUESTED", "CANCELLED")
    ]
    assert any(p.get("client_ref") == "unfillable-limit-1" for p in p_cancels)

    # 2. Run Replay session via CLI
    cli = CliRunner()
    res = cli.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "replay",
            "--session",
            paper_dir.name,
            "--run",
            "demo_paper_fast",
        ],
    )
    assert res.exit_code == 0, f"Replay CLI failed: {res.output}"

    # Find replay results directory
    results_dir = tmp_project / "results"
    replay_folders = [f for f in results_dir.glob("demo_paper_fast_replay_*") if f.is_dir()]
    assert len(replay_folders) >= 1
    replay_dir = sorted(replay_folders)[-1]

    # 3. Compare runs with compare_runs
    is_equal, diff = compare_runs(tmp_project, paper_dir.name, replay_dir.name)
    assert is_equal is True, f"Replay not equal to paper session: {diff}"

    # 4. Explicit verification of metrics, open positions, and equity points
    r_metrics = json.loads((replay_dir / "metrics.json").read_text(encoding="utf-8"))
    assert p_metrics["trades"] == r_metrics["trades"]
    assert p_metrics["open_positions"] == r_metrics["open_positions"]
    assert p_metrics["win_rate"] == r_metrics["win_rate"]
    assert math.isclose(p_metrics["max_drawdown"], r_metrics["max_drawdown"], abs_tol=1e-5)

    # Compare equity.csv row-for-row
    p_equity_file = paper_dir / "equity.csv"
    r_equity_file = replay_dir / "equity.csv"
    assert p_equity_file.is_file()
    assert r_equity_file.is_file()

    with (
        p_equity_file.open(newline="", encoding="utf-8") as f_p,
        r_equity_file.open(newline="", encoding="utf-8") as f_r,
    ):
        p_rows = list(csv.DictReader(f_p))
        r_rows = list(csv.DictReader(f_r))

    assert len(p_rows) == len(r_rows)
    assert len(p_rows) > 0
    for idx, (p_row, r_row) in enumerate(zip(p_rows, r_rows, strict=True)):
        assert math.isclose(float(p_row["equity"]), float(r_row["equity"]), abs_tol=1e-4), (
            f"Row {idx} equity mismatch: {p_row['equity']} != {r_row['equity']}"
        )
        assert math.isclose(float(p_row["drawdown"]), float(r_row["drawdown"]), abs_tol=1e-4), (
            f"Row {idx} drawdown mismatch: {p_row['drawdown']} != {r_row['drawdown']}"
        )
