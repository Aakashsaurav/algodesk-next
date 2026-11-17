"""Tests for Steps 8 and 9: HistoricalFeed, BarQuoteSynth, and event ordering."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from algodesk.api.market import Bar, DataSlice, InstrumentId, Quote
from algodesk.data.bar_synth import BarQuoteSynth
from algodesk.data.historical_feed import HistoricalFeed


def test_bar_quote_synth_up_and_down_bars() -> None:
    synth = BarQuoteSynth(participation_pct=10.0, price_tick=0.01)

    t0 = datetime(2026, 11, 10, 9, 15, tzinfo=UTC)
    t1 = datetime(2026, 11, 10, 15, 30, tzinfo=UTC)

    # 1. Up bar: close (105.0) >= open (100.0) -> O, L, H, C
    up_bar = Bar(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        timeframe="1d",
        start_ts=t0,
        end_ts=t1,
        open=100.0,
        high=110.0,
        low=98.0,
        close=105.0,
        volume=1000,
        complete=True,
    )
    q_o, q_l, q_h, q_c = synth.synthesize(up_bar)

    assert q_o.ltp == 100.0
    assert q_l.ltp == 98.0
    assert q_h.ltp == 110.0
    assert q_c.ltp == 105.0

    # Participation depth: 10% of 1000 = 100
    assert q_o.bids[0].qty == 100
    assert q_o.asks[0].qty == 100

    # Chronological timestamps inside the bar
    assert q_o.recv_ts == t0
    assert q_l.recv_ts < q_h.recv_ts
    assert q_h.recv_ts < q_c.recv_ts
    assert q_c.recv_ts == t1

    # 2. Down bar: close (95.0) < open (100.0) -> O, H, L, C
    down_bar = Bar(
        instrument=InstrumentId("NSE:EQ:TCS"),
        timeframe="1d",
        start_ts=t0,
        end_ts=t1,
        open=100.0,
        high=102.0,
        low=90.0,
        close=95.0,
        volume=500,
        complete=True,
    )
    dq_o, dq_h, dq_l, dq_c = synth.synthesize(down_bar)

    assert dq_o.ltp == 100.0
    assert dq_h.ltp == 102.0
    assert dq_l.ltp == 90.0
    assert dq_c.ltp == 95.0
    assert dq_o.bids[0].qty == 50  # 10% of 500


def test_historical_feed_cache_only_and_warmup() -> None:
    mock_ds = MagicMock()

    t_warmup = datetime(2026, 11, 8, 3, 45, tzinfo=UTC)
    t_start = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
    t_end = datetime(2026, 11, 10, 23, 59, 59, 999999, tzinfo=UTC)

    inst_rel = InstrumentId("NSE:EQ:RELIANCE")
    inst_tcs = InstrumentId("NSE:EQ:TCS")

    warmup_bar_rel = Bar(
        instrument=inst_rel,
        timeframe="1d",
        start_ts=t_warmup,
        end_ts=t_warmup + timedelta(hours=6),
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=100,
    )
    active_bar_rel = Bar(
        instrument=inst_rel,
        timeframe="1d",
        start_ts=t_start,
        end_ts=t_end,
        open=102.0,
        high=105.0,
        low=101.0,
        close=104.0,
        volume=200,
    )
    active_bar_tcs = Bar(
        instrument=inst_tcs,
        timeframe="1d",
        start_ts=t_start,
        end_ts=t_end,
        open=200.0,
        high=205.0,
        low=198.0,
        close=202.0,
        volume=300,
    )

    def mock_get(series: Any, start: Any, end: Any, **kwargs: Any) -> DataSlice:
        # Guarantee cache_only is always True
        assert kwargs.get("cache_only") is True
        s_str = str(series)
        if start < t_start:
            # Warm-up request
            bars = [warmup_bar_rel] if "RELIANCE" in s_str else []
        else:
            # Active window request
            bars = [active_bar_rel] if "RELIANCE" in s_str else [active_bar_tcs]
        return DataSlice(
            key=MagicMock(),
            bars=tuple(bars),
            start=start,
            end=end,
        )

    mock_ds.get.side_effect = mock_get

    feed = HistoricalFeed(
        data_service=mock_ds,
        instruments=[inst_tcs, inst_rel],
        start=date(2026, 11, 10),
        end=date(2026, 11, 10),
        timeframe="1d",
        warmup_bars_count=10,
    )

    # Check warm-up read before start
    assert len(feed.warmup_bars) == 1
    assert feed.warmup_bars[0].instrument == inst_rel

    # Check active bars merged by timestamp, ties broken by instrument id
    assert len(feed.bars) == 2
    # RELIANCE ("NSE:EQ:RELIANCE") precedes TCS ("NSE:EQ:TCS") alphabetically
    assert feed.bars[0].instrument == inst_rel
    assert feed.bars[1].instrument == inst_tcs


def test_historical_feed_event_ordering_and_same_timestamp_rules() -> None:
    mock_ds = MagicMock()
    t_start = datetime(2026, 11, 10, 3, 45, tzinfo=UTC)
    t_end = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)

    inst = InstrumentId("NSE:EQ:RELIANCE")
    bar = Bar(
        instrument=inst,
        timeframe="1d",
        start_ts=t_start,
        end_ts=t_end,
        open=100.0,
        high=105.0,
        low=98.0,
        close=103.0,
        volume=1000,
    )

    mock_ds.get.return_value = DataSlice(
        key=MagicMock(),
        bars=(bar,),
        start=t_start,
        end=t_end,
    )

    feed = HistoricalFeed(
        data_service=mock_ds,
        instruments=[inst],
        start=date(2026, 11, 10),
        end=date(2026, 11, 10),
        timeframe="1d",
        warmup_bars_count=0,
    )

    events = [item[0] for item in feed.iter_with_seq()]
    assert len(events) == 5  # 4 quotes + 1 bar

    q_o, q_l, q_h, q_c, b_ev = events
    assert isinstance(q_o, Quote)
    assert isinstance(q_l, Quote)
    assert isinstance(q_h, Quote)
    assert isinstance(q_c, Quote)
    assert isinstance(b_ev, Bar)

    # Same timestamp order verification:
    # Closing quote of bar t comes at t_end
    assert q_c.recv_ts == t_end
    # Bar completion event comes at t_end, AFTER closing quote
    assert b_ev.end_ts == t_end
