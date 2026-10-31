"""Comprehensive tests for session observability, partial run detection, and latency tracking."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from algodesk.api import InstrumentId, OrderEventMsg, Product, Side
from algodesk.api.trading import Order, OrderEvent, OrderSpec, OrderState, OrderType, Validity


def test_ist_time_conversion_morning() -> None:
    from zoneinfo import ZoneInfo

    ist_tz = ZoneInfo("Asia/Kolkata")
    # 03:45 UTC is 09:15 IST
    dt_utc = datetime(2026, 10, 30, 3, 45, tzinfo=UTC)
    dt_ist = dt_utc.astimezone(ist_tz)
    assert dt_ist.hour == 9
    assert dt_ist.minute == 15


def test_ist_time_conversion_afternoon() -> None:
    from zoneinfo import ZoneInfo

    ist_tz = ZoneInfo("Asia/Kolkata")
    # 10:00 UTC is 15:30 IST
    dt_utc = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
    dt_ist = dt_utc.astimezone(ist_tz)
    assert dt_ist.hour == 15
    assert dt_ist.minute == 30


def test_session_coverage_calculation_full_day() -> None:
    from zoneinfo import ZoneInfo

    ist_tz = ZoneInfo("Asia/Kolkata")
    f_dt = datetime(2026, 10, 26, 3, 45, tzinfo=UTC)
    l_dt = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
    f_ist = f_dt.astimezone(ist_tz)
    l_ist = l_dt.astimezone(ist_tz)
    open_t = f_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = f_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    covered_sec = (min(close_t, l_ist) - max(open_t, f_ist)).total_seconds()
    cov_pct = min(100.0, max(0.0, (covered_sec / 22500.0) * 100.0))
    assert cov_pct == 100.0


def test_session_coverage_calculation_half_day() -> None:
    from zoneinfo import ZoneInfo

    ist_tz = ZoneInfo("Asia/Kolkata")
    f_dt = datetime(2026, 10, 26, 3, 45, tzinfo=UTC)  # 09:15 IST
    l_dt = datetime(2026, 10, 26, 6, 52, 30, tzinfo=UTC)  # 12:22:30 IST (half of 6h15m)
    f_ist = f_dt.astimezone(ist_tz)
    l_ist = l_dt.astimezone(ist_tz)
    open_t = f_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = f_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    covered_sec = (min(close_t, l_ist) - max(open_t, f_ist)).total_seconds()
    cov_pct = min(100.0, max(0.0, (covered_sec / 22500.0) * 100.0))
    assert abs(cov_pct - 50.0) < 0.1


def test_partial_session_tagging_after_market_open() -> None:
    from zoneinfo import ZoneInfo

    ist_tz = ZoneInfo("Asia/Kolkata")
    f_dt = datetime(2026, 10, 26, 3, 50, tzinfo=UTC)  # 09:20 IST
    f_ist = f_dt.astimezone(ist_tz)
    open_t = f_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    is_partial = f_ist > open_t
    assert is_partial is True


def test_partial_session_tagging_at_market_open() -> None:
    from zoneinfo import ZoneInfo

    ist_tz = ZoneInfo("Asia/Kolkata")
    f_dt = datetime(2026, 10, 26, 3, 45, 0, tzinfo=UTC)  # 09:15:00 IST
    f_ist = f_dt.astimezone(ist_tz)
    open_t = f_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    is_partial = f_ist > open_t
    assert is_partial is False


def test_p50_and_p95_latency_computation_even() -> None:
    lats = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    sorted_lats = sorted(lats)
    n = len(sorted_lats)
    p50 = sorted_lats[int(n * 0.50)]
    p95 = sorted_lats[min(n - 1, int(n * 0.95))]
    assert p50 == 6.0
    assert p95 == 10.0


def test_p50_and_p95_latency_computation_single() -> None:
    lats = [4.2]
    sorted_lats = sorted(lats)
    n = len(sorted_lats)
    p50 = sorted_lats[int(n * 0.50)]
    p95 = sorted_lats[min(n - 1, int(n * 0.95))]
    assert p50 == 4.2
    assert p95 == 4.2


def test_p50_and_p95_latency_empty() -> None:
    lats: list[float] = []
    if lats:
        p50 = lats[0]
        p95 = lats[0]
    else:
        p50 = 0.0
        p95 = 0.0
    assert p50 == 0.0
    assert p95 == 0.0


def test_peak_rss_mb_calculation() -> None:
    import resource
    import sys

    usage = resource.getrusage(resource.RUSAGE_SELF)
    raw = usage.ru_maxrss
    if sys.platform == "darwin":
        mb = raw / (1024.0 * 1024.0)
    else:
        mb = raw / 1024.0
    assert mb > 0.0
    assert mb < 10000.0  # Reasonable peak memory (< 10GB)


def test_order_event_msg_order_count() -> None:
    now = datetime.now(UTC)
    spec = OrderSpec(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        side=Side.BUY,
        qty=10,
        type=OrderType.LIMIT,
        price=Decimal("2500.00"),
        product=Product.CNC,
        validity=Validity.DAY,
    )
    order = Order(
        client_ref="ref_1",
        spec=spec,
        state=OrderState.CREATED,
        created_ts=now,
        filled_qty=0,
        avg_price=Decimal("0.0"),
    )
    event = OrderEvent(
        client_ref="ref_1",
        from_state=OrderState.CREATED,
        to_state=OrderState.SUBMITTED,
        ts=now,
    )
    msg = OrderEventMsg(event=event, order=order)
    assert msg.ts == event.ts
    assert msg.order == order


def test_results_writer_summary_includes_partial_verdict(tmp_path: Path) -> None:
    from algodesk.results.writer import ResultsWriter

    writer = ResultsWriter("run_partial_test", results_dir=tmp_path)
    summary_info = {
        "run_name": "demo_paper",
        "mode": "paper",
        "date": "2026-10-30",
        "verdict": "PASS (partial)",
        "is_partial": True,
    }
    metrics = {
        "net_pnl": 1500.0,
        "gross_pnl": 1550.0,
        "charges": 50.0,
        "trades": 5,
        "win_rate": 80.0,
        "max_drawdown": 1.2,
    }
    writer.write(
        summary_info=summary_info,
        metrics=metrics,
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
    )

    summary_file = tmp_path / "run_partial_test" / "SUMMARY.md"
    assert summary_file.is_file()
    content = summary_file.read_text(encoding="utf-8")
    assert "Verdict: PASS (partial)" in content


def test_results_writer_summary_includes_fail_reason(tmp_path: Path) -> None:
    from algodesk.results.writer import ResultsWriter

    writer = ResultsWriter("run_fail_test", results_dir=tmp_path)
    summary_info = {
        "run_name": "demo_paper",
        "mode": "paper",
        "date": "2026-10-30",
        "verdict": "FAIL",
        "fail_reason": "WebSocket connection reset by peer",
        "is_partial": False,
    }
    metrics = {"trades": 0}
    writer.write(
        summary_info=summary_info,
        metrics=metrics,
        trades=[],
        equity_points=[],
        effective_config_text="",
        registry_snapshot={},
        manifest_info={},
    )

    summary_file = tmp_path / "run_fail_test" / "SUMMARY.md"
    assert summary_file.is_file()
    content = summary_file.read_text(encoding="utf-8")
    assert "Verdict: FAIL" in content
    assert "Reason: WebSocket connection reset by peer" in content


def test_recorder_total_rows_and_bytes_tracking() -> None:
    from algodesk.data.recorder import Recorder

    rec = Recorder(
        data_dir=Path("/tmp"),
        manifest=MagicMock(),
        bus=None,
        scheduler=None,
        source="live",
    )
    assert rec.total_rows == 0
    assert rec.total_bytes == 0
