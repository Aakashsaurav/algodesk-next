"""Tests for M1-9: IST display everywhere, session metrics, demo realism, volume visibility."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from plugins.strategies.sma_cross.config_schema import SMACrossConfig
from typer.testing import CliRunner

from algodesk.api import Fill, InstrumentId, Product, Side
from algodesk.cli.app import app
from algodesk.fees.engine import FeeEngine
from algodesk.logs.formatting import IST, format_ist, to_ist
from algodesk.logs.setup import JsonLogFormatter, TextLogFormatter
from algodesk.ops.checks import _check_ticks_projection
from algodesk.results.roundtrips import compute_round_trip_metrics
from algodesk.results.writer import ResultsWriter

# ---------------------------------------------------------------------------
# CP1 — IST Formatting Helper & Outputs (Steps 2, 4)
# ---------------------------------------------------------------------------


def test_format_ist_1830z_daily_candle_displays_next_day_ist_date() -> None:
    """A daily candle starting at 18:30Z UTC displays the next calendar day in IST."""
    candle_ts = datetime(2026, 10, 25, 18, 30, 0, tzinfo=UTC)
    res_date = format_ist(candle_ts, date_only=True)
    assert res_date == "2026-10-26"
    assert "+00:00" not in res_date


def test_format_ist_timestamp_contains_ist_and_no_utc_offset() -> None:
    """Formatted timestamps end with IST suffix and contain no +00:00 or Z."""
    candle_ts = datetime(2026, 10, 25, 18, 30, 0, tzinfo=UTC)
    res_ts = format_ist(candle_ts)
    assert res_ts.endswith(" IST")
    assert res_ts == "2026-10-26 00:00:00 IST"
    assert "+00:00" not in res_ts
    assert "Z" not in res_ts


def test_format_ist_datetime_utc_conversion() -> None:
    """UTC datetime converts with exact +5:30 offset."""
    dt_utc = datetime(2026, 11, 2, 9, 15, 0, tzinfo=UTC)
    res = format_ist(dt_utc)
    assert res == "2026-11-02 14:45:00 IST"


def test_format_ist_naive_datetime() -> None:
    """Naive datetimes are assumed UTC and converted properly."""
    dt_naive = datetime(2026, 11, 2, 3, 45, 0)
    res = format_ist(dt_naive)
    assert res == "2026-11-02 09:15:00 IST"


def test_format_ist_iso_strings() -> None:
    """ISO strings with Z or offsets parse and convert to IST."""
    iso_z = "2026-11-02T03:45:00Z"
    assert format_ist(iso_z) == "2026-11-02 09:15:00 IST"

    iso_utc = "2026-11-02T03:45:00+00:00"
    assert format_ist(iso_utc) == "2026-11-02 09:15:00 IST"


def test_format_ist_epoch_seconds() -> None:
    """Epoch seconds convert accurately to IST."""
    dt = datetime(2026, 10, 26, 3, 45, 0, tzinfo=UTC)
    epoch = dt.timestamp()
    assert format_ist(epoch) == "2026-10-26 09:15:00 IST"


def test_format_ist_include_ms() -> None:
    """Milliseconds option includes 3-digit millisecond value."""
    dt = datetime(2026, 10, 26, 3, 45, 0, 123456, tzinfo=UTC)
    res = format_ist(dt, include_ms=True)
    assert res == "2026-10-26 09:15:00.123 IST"


def test_format_ist_none_and_empty() -> None:
    """None, empty, or unparseable input returns N/A."""
    assert format_ist(None) == "N/A"
    assert format_ist("") == "N/A"
    assert format_ist("not-a-date") == "N/A"


def test_format_ist_already_ist_string_idempotent() -> None:
    """String ending in IST parses idempotently without double-shifting."""
    val = "2026-11-02 09:15:00 IST"
    assert format_ist(val) == "2026-11-02 09:15:00 IST"


def test_to_ist_conversion() -> None:
    """to_ist returns ZoneInfo('Asia/Kolkata') aware datetime."""
    dt = to_ist("2026-10-26T03:45:00Z")
    assert dt is not None
    assert dt.tzinfo == IST
    assert dt.hour == 9
    assert dt.minute == 15


def test_json_log_formatter_includes_ts_and_ts_ist() -> None:
    """JsonLogFormatter includes ts (UTC) and ts_ist (IST)."""
    fmt = JsonLogFormatter("run_test")
    import logging

    rec = logging.LogRecord("test", logging.INFO, "test.py", 1, "test message", (), None)
    out_json = json.loads(fmt.format(rec))
    assert "ts" in out_json
    assert out_json["ts"].endswith("Z")
    assert "ts_ist" in out_json
    assert out_json["ts_ist"].endswith(" IST")
    assert "+00:00" not in out_json["ts_ist"]


def test_text_log_formatter_includes_ts_and_ts_ist() -> None:
    """TextLogFormatter includes ts (UTC) and ts_ist (IST)."""
    fmt = TextLogFormatter("run_test")
    import logging

    rec = logging.LogRecord("test", logging.INFO, "test.py", 1, "text message", (), None)
    out_txt = fmt.format(rec)
    assert "Z" in out_txt
    assert "IST" in out_txt
    assert "[run=run_test]" in out_txt or "(run=run_test)" in out_txt


def test_results_trades_csv_contains_ts_and_ts_ist(tmp_path: Path) -> None:
    """trades.csv output keeps ts (UTC) and adds ts_ist (IST)."""
    writer = ResultsWriter("run_csv_test", results_dir=tmp_path)
    trades = [
        {
            "trade_id": "T1",
            "fill_id": "F1",
            "client_ref": "R1",
            "instrument": "NSE:EQ:RELIANCE",
            "side": "BUY",
            "qty": 1,
            "price": 2500.0,
            "charges": 1.5,
            "ts": "2026-10-26T04:00:00+00:00",
        }
    ]
    writer.write(
        summary_info={"run_name": "run_csv_test", "mode": "paper"},
        metrics={},
        trades=trades,
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
    )
    p = tmp_path / "run_csv_test" / "trades.csv"
    assert p.is_file()
    lines = p.read_text().splitlines()
    header = lines[0].split(",")
    assert "ts" in header
    assert "ts_ist" in header
    data = lines[1].split(",")
    ts_idx = header.index("ts")
    ts_ist_idx = header.index("ts_ist")
    assert data[ts_idx] == "2026-10-26T04:00:00+00:00"
    assert data[ts_ist_idx] == "2026-10-26 09:30:00 IST"


def test_results_equity_csv_contains_ts_and_ts_ist(tmp_path: Path) -> None:
    """equity.csv output keeps ts (UTC) and adds ts_ist (IST)."""
    writer = ResultsWriter("run_eq_test", results_dir=tmp_path)
    equity = [{"ts": "2026-10-26T04:00:00+00:00", "equity": 100000.0}]
    writer.write(
        summary_info={"run_name": "run_eq_test", "mode": "paper"},
        metrics={},
        trades=[],
        equity_points=equity,
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
    )
    p = tmp_path / "run_eq_test" / "equity.csv"
    assert p.is_file()
    lines = p.read_text().splitlines()
    header = lines[0].split(",")
    assert "ts" in header
    assert "ts_ist" in header
    data = lines[1].split(",")
    assert data[header.index("ts_ist")] == "2026-10-26 09:30:00 IST"


def test_summary_md_displays_ist_and_no_utc_offset(tmp_path: Path) -> None:
    """SUMMARY.md renders execution date in IST with no +00:00."""
    writer = ResultsWriter("run_sum_test", results_dir=tmp_path)
    writer.write(
        summary_info={
            "run_name": "run_sum_test",
            "mode": "paper",
            "date": "2026-10-26",
            "start_ist": "2026-10-26 09:15:00 IST",
        },
        metrics={"net_pnl": 10.0, "trades": 1, "win_rate": 100.0},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
    )
    content = (tmp_path / "run_sum_test" / "SUMMARY.md").read_text()
    assert "IST" in content
    assert "+00:00" not in content


def test_session_md_displays_ist_and_no_utc_offset(tmp_path: Path) -> None:
    """session.md summary renders timestamps with IST suffix."""
    writer = ResultsWriter("run_sess_test", results_dir=tmp_path)
    writer.write(
        summary_info={"run_name": "run_sess_test", "mode": "paper"},
        metrics={"first_tick_ist": "2026-10-26 09:15:00 IST"},
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
    )
    content = (tmp_path / "run_sess_test" / "session.md").read_text()
    assert "IST" in content
    assert "+00:00" not in content


# ---------------------------------------------------------------------------
# CP1 — Replay Run ID Uniqueness & Console State (Step 3)
# ---------------------------------------------------------------------------


def test_replay_run_id_unique_per_replay_actual_start_time(tmp_project: Path) -> None:
    """Replay run ids are unique per invocation based on actual wall-clock time."""
    from algodesk.config.snapshot import build_effective
    from algodesk.engine.runner import SessionRunner
    from algodesk.ops.runtime import open_runtime

    with open_runtime(tmp_project, ensure=False) as rt:
        eff = build_effective(tmp_project, run_name="demo_paper_fast")
        r1 = SessionRunner(rt, run_config=eff.run, mode="replay", replay_dates=["2026-10-26"])
        r2 = SessionRunner(rt, run_config=eff.run, mode="replay", replay_dates=["2026-10-26"])
        assert r1.run_id != r2.run_id
        assert "034500" not in r1.run_id
        assert "034500" not in r2.run_id
        assert r1.mode_name == "replay"
        assert r2.mode_name == "replay"


def test_replay_run_id_stays_utc(tmp_project: Path) -> None:
    """Run IDs stay in UTC timestamp format."""
    from algodesk.config.snapshot import build_effective
    from algodesk.engine.runner import SessionRunner
    from algodesk.ops.runtime import open_runtime

    with open_runtime(tmp_project, ensure=False) as rt:
        eff = build_effective(tmp_project, run_name="demo_paper_fast")
        runner = SessionRunner(rt, run_config=eff.run, mode="replay", replay_dates=["2026-10-26"])
        # Format check: demo_paper_fast_replay_YYYYMMDD_HHMMSS_<rand>
        parts = runner.run_id.split("_")
        assert "replay" in parts
        assert len(parts) >= 4


def test_console_state_label_for_replay_is_replay(tmp_project: Path) -> None:
    """The console state label for replay mode is 'replay', never 'backtest'."""
    from algodesk.config.snapshot import build_effective
    from algodesk.engine.runner import SessionRunner
    from algodesk.ops.runtime import open_runtime

    with open_runtime(tmp_project, ensure=False) as rt:
        eff = build_effective(tmp_project, run_name="demo_paper_fast")
        runner = SessionRunner(rt, run_config=eff.run, mode="replay", replay_dates=["2026-10-26"])
        assert runner.mode_name == "replay"


# ---------------------------------------------------------------------------
# CP2 — Metrics & Demo Realism (Steps 5, 6, 7)
# ---------------------------------------------------------------------------


def test_compute_round_trip_metrics_empty_fills() -> None:
    """Empty fills produce 0 round trips and 0.0 win rate."""
    res = compute_round_trip_metrics([])
    assert res["round_trips"] == 0
    assert res["win_rate"] == 0.0
    assert res["gross_win_rate"] == 0.0


def test_compute_round_trip_metrics_all_winners() -> None:
    """Losing no trades yields 100% win rate."""
    fills = [
        {"instrument": "NSE:EQ:INFY", "side": "BUY", "qty": 10, "price": 1000.0, "charges": 5.0},
        {"instrument": "NSE:EQ:INFY", "side": "SELL", "qty": 10, "price": 1050.0, "charges": 5.0},
    ]
    res = compute_round_trip_metrics(fills)
    assert res["round_trips"] == 1
    assert res["winning_round_trips"] == 1
    assert res["win_rate"] == 100.0
    assert res["gross_win_rate"] == 100.0


def test_compute_round_trip_metrics_all_losers() -> None:
    """All losing round trips yield 0.0% win rate."""
    fills = [
        {"instrument": "NSE:EQ:TCS", "side": "BUY", "qty": 5, "price": 3000.0, "charges": 3.0},
        {"instrument": "NSE:EQ:TCS", "side": "SELL", "qty": 5, "price": 2950.0, "charges": 3.0},
    ]
    res = compute_round_trip_metrics(fills)
    assert res["round_trips"] == 1
    assert res["winning_round_trips"] == 0
    assert res["win_rate"] == 0.0
    assert res["gross_win_rate"] == 0.0


def test_compute_round_trip_metrics_gross_winner_net_loser() -> None:
    """A trade with small gross gain wiped out by charges is a net loss."""
    fills = [
        {"instrument": "NSE:EQ:TCS", "side": "BUY", "qty": 1, "price": 2526.3, "charges": 3.0},
        {"instrument": "NSE:EQ:TCS", "side": "SELL", "qty": 1, "price": 2527.1, "charges": 18.55},
    ]
    res = compute_round_trip_metrics(fills)
    assert res["round_trips"] == 1
    assert res["gross_winning_round_trips"] == 1
    assert res["winning_round_trips"] == 0
    assert res["gross_win_rate"] == 100.0
    assert res["win_rate"] == 0.0


def test_compute_round_trip_metrics_29_fill_pattern() -> None:
    """29-fill pattern with -7.25 gross P&L must NOT give 100% win rate."""
    csv_path = Path("results/demo_paper_fast_paper_20261102_055142_356d14/trades.csv")
    assert csv_path.exists(), "Reference 29-fill trades.csv must exist"
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fills = list(reader)

    assert len(fills) == 29
    res = compute_round_trip_metrics(fills)
    assert res["round_trips"] == 13
    assert res["winning_round_trips"] == 0
    assert res["win_rate"] == 0.0
    assert res["gross_win_rate"] < 40.0


def test_compute_round_trip_metrics_partial_lot_closes() -> None:
    """FIFO partial lot matching handles multi-step position closes correctly."""
    fills = [
        {
            "instrument": "NSE:EQ:RELIANCE",
            "side": "BUY",
            "qty": 10,
            "price": 2000.0,
            "charges": 10.0,
        },
        {
            "instrument": "NSE:EQ:RELIANCE",
            "side": "SELL",
            "qty": 4,
            "price": 2050.0,
            "charges": 4.0,
        },
        {
            "instrument": "NSE:EQ:RELIANCE",
            "side": "SELL",
            "qty": 6,
            "price": 1950.0,
            "charges": 6.0,
        },
    ]
    res = compute_round_trip_metrics(fills)
    assert res["round_trips"] == 2
    assert res["winning_round_trips"] == 1
    assert res["win_rate"] == 50.0


def test_compute_round_trip_metrics_short_round_trip() -> None:
    """Short trades opened with SELL and closed with BUY compute profit accurately."""
    fills = [
        {
            "instrument": "NSE:EQ:HDFCBANK",
            "side": "SELL",
            "qty": 5,
            "price": 1500.0,
            "charges": 2.0,
        },
        {"instrument": "NSE:EQ:HDFCBANK", "side": "BUY", "qty": 5, "price": 1450.0, "charges": 2.0},
    ]
    res = compute_round_trip_metrics(fills)
    assert res["round_trips"] == 1
    assert res["winning_round_trips"] == 1
    assert res["win_rate"] == 100.0


def test_compute_round_trip_metrics_multi_instrument() -> None:
    """Round trips across distinct instruments do not cross-match."""
    fills = [
        {"instrument": "NSE:EQ:TCS", "side": "BUY", "qty": 1, "price": 3000.0, "charges": 1.0},
        {"instrument": "NSE:EQ:INFY", "side": "BUY", "qty": 1, "price": 1500.0, "charges": 1.0},
        {"instrument": "NSE:EQ:TCS", "side": "SELL", "qty": 1, "price": 3100.0, "charges": 1.0},
        {"instrument": "NSE:EQ:INFY", "side": "SELL", "qty": 1, "price": 1400.0, "charges": 1.0},
    ]
    res = compute_round_trip_metrics(fills)
    assert res["round_trips"] == 2
    assert res["winning_round_trips"] == 1
    assert res["win_rate"] == 50.0


def test_sma_cross_config_default_product_is_mis() -> None:
    """SMACrossConfig default product type is 'mis'."""
    cfg = SMACrossConfig()
    assert cfg.product == "mis"


def test_demo_paper_fast_uses_mis_product() -> None:
    """demo_paper_fast configuration explicitly configures product = 'MIS'."""
    import tomllib

    with open("config/runs/demo_paper_fast.toml", "rb") as f:
        data = tomllib.load(f)
    strategies = data.get("strategies", [])
    assert len(strategies) > 0
    sma = strategies[0]
    assert sma.get("plugin") == "sma_cross"
    assert sma.get("params", {}).get("product") == "MIS"


def test_mis_round_trip_has_no_dp_charge() -> None:
    """Equity MIS round trip incurs zero Depository Participant (DP) charge."""
    import tomllib

    from algodesk.fees import Schedule

    t_buy = datetime(2026, 10, 26, 4, 0, 0, tzinfo=UTC)
    t_sell = datetime(2026, 10, 26, 5, 0, 0, tzinfo=UTC)

    f_buy = Fill(
        fill_id="F1",
        client_ref="REF1",
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        qty=10,
        price=Decimal("2500.0"),
        ts=t_buy,
        product=Product.MIS,
    )
    f_sell = Fill(
        fill_id="F2",
        client_ref="REF2",
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.SELL,
        qty=10,
        price=Decimal("2550.0"),
        ts=t_sell,
        product=Product.MIS,
    )

    with open("plugins/fees/zerodha_nse/schedule.toml", "rb") as f:
        sch = Schedule.model_validate(tomllib.load(f))

    charges = FeeEngine.charges_for_fills([f_buy, f_sell], sch)
    assert len(charges) == 2
    # Verify DP charge component on sell leg is 0
    sell_chg = charges[1]
    assert sell_chg.dp == Decimal("0")


def test_cnc_round_trip_has_dp_charge() -> None:
    """Equity CNC sell incurs DP charge of ~15.93 (13.50 + 18% GST)."""
    import tomllib

    from algodesk.fees import Schedule

    t_sell = datetime(2026, 10, 26, 5, 0, 0, tzinfo=UTC)

    f_sell = Fill(
        fill_id="F2",
        client_ref="REF2",
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.SELL,
        qty=10,
        price=Decimal("2550.0"),
        ts=t_sell,
        product=Product.CNC,
    )

    with open("plugins/fees/zerodha_nse/schedule.toml", "rb") as f:
        sch = Schedule.model_validate(tomllib.load(f))

    charges = FeeEngine.charges_for_fills([f_sell], sch)
    sell_chg = charges[0]
    assert sell_chg.dp > Decimal("10.0")


# ---------------------------------------------------------------------------
# CP3 — Volume Visibility & Doctor Check (Step 8)
# ---------------------------------------------------------------------------


def test_session_report_prints_both_latencies_and_recorder_volume(tmp_project: Path) -> None:
    """Session report prints both tick-to-intent latency and bar_close_delay."""
    runner = CliRunner()
    results_dir = tmp_project / "results"
    run_dir = results_dir / "test_report_vol_001"
    run_dir.mkdir(parents=True, exist_ok=True)

    metrics = {
        "date": "2026-10-26",
        "mode": "paper",
        "first_tick_ist": "2026-10-26 09:15:00 IST",
        "last_tick_ist": "2026-10-26 15:30:00 IST",
        "session_coverage_pct": 100.0,
        "win_rate": 45.5,
        "gross_win_rate": 60.0,
        "signals": 10,
        "orders": 6,
        "fills": 6,
        "p95_latency_ms": 1.25,
        "p95_bar_close_delay_ms": 250.5,
        "peak_rss_mb": 120.0,
        "recorder_rows": 20000,
        "recorder_bytes": 1048576,
        "recorder_mb_per_inst_hour": 0.05,
        "projected_full_day_mb": 1.88,
        "journal_growth_bytes": 1024,
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (run_dir / "SUMMARY.md").write_text("# Summary\nVerdict: PASS\n", encoding="utf-8")

    res = runner.invoke(
        app, ["--root", str(tmp_project), "session", "report", "test_report_vol_001"]
    )
    assert res.exit_code == 0
    out = res.output

    # Check latencies
    assert "Platform (p95):" in out
    assert "1.25 ms" in out
    assert "Bar-Close Delay (p95):" in out
    assert "250.50 ms" in out

    # Check recorder volume visibility
    assert "Recorder Volume:" in out
    assert "0.05 MB / inst-hr" in out
    assert "Projected full-day: 1.88 MB" in out

    # Check win rate
    assert "Win Rate:" in out
    assert "45.5%" in out


def test_doctor_ticks_projection_check_passes_when_space_ample(tmp_project: Path) -> None:
    """Doctor ticks projection passes when ample disk space is available."""
    from algodesk.ops.runtime import open_runtime

    with open_runtime(tmp_project, ensure=False) as rt:
        res = _check_ticks_projection(rt)
        assert res.ok
        assert "within" in res.message


def test_doctor_ticks_projection_check_warns_when_projected_exceeds_fraction(
    tmp_project: Path,
) -> None:
    """Doctor warns when projected 10 days of ticks exceed registry fraction of free disk."""
    import shutil

    from algodesk.ops.runtime import open_runtime

    with open_runtime(tmp_project, ensure=False) as rt:
        # Mock disk space to very low free space (e.g. 100 MB free)
        fake_usage = shutil._ntuple_diskusage(100 * 1024**3, 99 * 1024**3, 100 * 1024 * 1024)
        with patch("shutil.disk_usage", return_value=fake_usage):
            res = _check_ticks_projection(rt)
            assert not res.ok
            assert "exceeds" in res.message
            assert res.fix is not None


def test_doctor_ticks_projection_reads_registry_param(tmp_project: Path) -> None:
    """Doctor check respects recorder.max_disk_fraction parameter."""
    from algodesk.ops.runtime import open_runtime

    with open_runtime(tmp_project, ensure=False) as rt:
        mock_params = MagicMock()
        mock_params.get.return_value = 1.0
        object.__setattr__(rt, "params", mock_params)
        res = _check_ticks_projection(rt)
        assert res.ok
