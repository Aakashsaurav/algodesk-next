"""Tests for algodesk.api.market — types, validation, and conversion helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from algodesk.api.market import (
    Bar,
    FeedStatus,
    IdParts,
    Instrument,
    InstrumentId,
    Level,
    Quote,
    SeriesKey,
    Tick,
    format_id,
    parse_id,
    to_price,
)

UTC = UTC


def test_parse_and_format_equity_id() -> None:
    raw = "NSE:EQ:RELIANCE"
    parts = parse_id(raw)
    assert parts.exchange == "NSE"
    assert parts.segment == "EQ"
    assert parts.symbol == "RELIANCE"
    assert parts.expiry is None
    assert parts.strike is None
    assert parts.option_type is None
    formatted = format_id(parts)
    assert formatted == raw


def test_parse_and_format_future_id() -> None:
    raw = "NSE:FUT:RELIANCE:2026-10-27"
    parts = parse_id(raw)
    assert parts.exchange == "NSE"
    assert parts.segment == "FUT"
    assert parts.symbol == "RELIANCE"
    assert parts.expiry == date(2026, 10, 27)
    assert parts.strike is None
    assert parts.option_type is None
    formatted = format_id(parts)
    assert formatted == raw


def test_parse_and_format_option_id() -> None:
    raw = "NSE:OPT:NIFTY:2026-10-29:24000:CE"
    parts = parse_id(raw)
    assert parts.exchange == "NSE"
    assert parts.segment == "OPT"
    assert parts.symbol == "NIFTY"
    assert parts.expiry == date(2026, 10, 29)
    assert parts.strike == Decimal("24000")
    assert parts.option_type == "CE"
    formatted = format_id(parts)
    assert formatted == raw


def test_parse_id_invalid_strings() -> None:
    invalid_cases = [
        "",
        "nse:eq:reliance",  # lowercase
        "NSE:EQ",  # only 2 parts
        "NSE:EQ:RELIANCE:2026-10-27:24000",  # 5 parts
        "NSE:EQ:RELIANCE:invalid-date",
        "NSE:OPT:NIFTY:2026-10-29:not-a-number:CE",
        "NSE:OPT:NIFTY:2026-10-29:24000:XX",  # invalid option type
    ]
    for case in invalid_cases:
        with pytest.raises(ValueError):
            parse_id(case)

    with pytest.raises(TypeError):
        parse_id(123)  # type: ignore[arg-type]


def test_id_parts_validation() -> None:
    with pytest.raises(ValueError, match="Exchange must be non-empty uppercase"):
        IdParts(exchange="nse", segment="EQ", symbol="RELIANCE")
    with pytest.raises(ValueError, match="Option type must be 'CE' or 'PE'"):
        IdParts(
            exchange="NSE",
            segment="OPT",
            symbol="NIFTY",
            expiry=date(2026, 10, 29),
            strike=Decimal("24000"),
            option_type="CALL",
        )
    with pytest.raises(ValueError, match="Strike must be a positive Decimal"):
        IdParts(
            exchange="NSE",
            segment="OPT",
            symbol="NIFTY",
            expiry=date(2026, 10, 29),
            strike=Decimal("-100"),
            option_type="CE",
        )
    with pytest.raises(ValueError, match="Option type must be specified when strike is present"):
        IdParts(
            exchange="NSE",
            segment="OPT",
            symbol="NIFTY",
            expiry=date(2026, 10, 29),
            strike=Decimal("24000"),
        )


def test_to_price_rounding() -> None:
    tick = Decimal("0.05")
    assert to_price(100.02, tick) == Decimal("100.00")
    assert to_price(100.03, tick) == Decimal("100.05")
    assert to_price(100.07, tick) == Decimal("100.05")
    assert to_price(100.08, tick) == Decimal("100.10")

    # Tick of 0.25
    tick_25 = Decimal("0.25")
    assert to_price(1523.10, tick_25) == Decimal("1523.00")
    assert to_price(1523.15, tick_25) == Decimal("1523.25")

    with pytest.raises(ValueError, match="tick must be a positive Decimal"):
        to_price(100.0, Decimal("0.0"))
    with pytest.raises(ValueError, match="tick must be a positive Decimal"):
        to_price(100.0, Decimal("-0.05"))
    with pytest.raises(ValueError, match="tick must be a positive Decimal"):
        to_price(100.0, 0.05)  # type: ignore[arg-type]


def test_instrument_validation() -> None:
    ins = Instrument(
        id=InstrumentId("NSE:EQ:INFY"),
        exchange="NSE",
        segment="EQ",
        symbol="INFY",
        kind="equity",
        lot_size=1,
        tick_size=Decimal("0.05"),
        isin="INE009A01021",
    )
    assert ins.id == "NSE:EQ:INFY"
    assert ins.lot_size == 1

    with pytest.raises(ValueError, match="lot_size must be positive"):
        Instrument(
            id=InstrumentId("NSE:EQ:INFY"),
            exchange="NSE",
            segment="EQ",
            symbol="INFY",
            kind="equity",
            lot_size=0,
            tick_size=Decimal("0.05"),
        )
    with pytest.raises(ValueError, match="tick_size must be a positive Decimal"):
        Instrument(
            id=InstrumentId("NSE:EQ:INFY"),
            exchange="NSE",
            segment="EQ",
            symbol="INFY",
            kind="equity",
            lot_size=1,
            tick_size=Decimal("-0.05"),
        )


def test_series_key_validation() -> None:
    sk = SeriesKey(
        instrument=InstrumentId("NSE:EQ:TCS"),
        kind="bars",
        timeframe="1m",
        adjustment="raw",
        source="upstox",
    )
    assert sk.timeframe == "1m"

    with pytest.raises(ValueError, match="Invalid series kind"):
        SeriesKey(
            instrument=InstrumentId("NSE:EQ:TCS"),
            kind="orderbook",
            timeframe="1m",
            adjustment="raw",
            source="upstox",
        )
    with pytest.raises(ValueError, match="Invalid adjustment"):
        SeriesKey(
            instrument=InstrumentId("NSE:EQ:TCS"),
            kind="bars",
            timeframe="1m",
            adjustment="split_only",
            source="upstox",
        )


def test_level_validation() -> None:
    lvl = Level(price=100.5, qty=25, orders=2)
    assert lvl.price == 100.5
    assert lvl.qty == 25
    assert lvl.orders == 2

    with pytest.raises(ValueError, match="price cannot be negative"):
        Level(price=-1.0, qty=10)
    with pytest.raises(ValueError, match="qty cannot be negative"):
        Level(price=10.0, qty=-5)
    with pytest.raises(ValueError, match="orders cannot be negative"):
        Level(price=10.0, qty=5, orders=-1)


def test_tick_validation() -> None:
    now = datetime.now(UTC)
    tick = Tick(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        exch_ts=now,
        recv_ts=now,
        ltp=2500.5,
        ltq=10,
        volume=10000,
        oi=None,
    )
    assert tick.ltp == 2500.5

    # Naive timestamp
    with pytest.raises(ValueError, match="must be timezone-aware UTC"):
        Tick(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            exch_ts=datetime(2026, 10, 23, 10, 0),
            recv_ts=now,
            ltp=2500.5,
            ltq=10,
            volume=10000,
        )

    # Non-UTC timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    with pytest.raises(ValueError, match="must be timezone-aware UTC"):
        Tick(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            exch_ts=datetime.now(ist),
            recv_ts=now,
            ltp=2500.5,
            ltq=10,
            volume=10000,
        )

    # Non-positive ltq
    with pytest.raises(ValueError, match="ltq must be positive"):
        Tick(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            exch_ts=now,
            recv_ts=now,
            ltp=2500.5,
            ltq=0,
            volume=10000,
        )


def test_quote_validation() -> None:
    now = datetime.now(UTC)
    bids = (
        Level(price=2500.0, qty=10, orders=1),
        Level(price=2499.5, qty=20, orders=2),
    )
    asks = (
        Level(price=2500.5, qty=15, orders=1),
        Level(price=2501.0, qty=25, orders=2),
    )
    quote = Quote(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        exch_ts=now,
        recv_ts=now,
        bids=bids,
        asks=asks,
        ltp=2500.25,
        volume=5000,
    )
    assert len(quote.bids) == 2

    # More than 5 levels
    too_many_bids = tuple(Level(price=2500.0 - i, qty=10) for i in range(6))
    with pytest.raises(ValueError, match="bids depth exceeds maximum 5 levels"):
        Quote(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            exch_ts=now,
            recv_ts=now,
            bids=too_many_bids,
            asks=(),
            ltp=2500.0,
            volume=100,
        )

    # Bids not descending
    unordered_bids = (
        Level(price=2499.0, qty=10),
        Level(price=2500.0, qty=10),
    )
    with pytest.raises(ValueError, match="Bids must be ordered descending by price"):
        Quote(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            exch_ts=now,
            recv_ts=now,
            bids=unordered_bids,
            asks=(),
            ltp=2500.0,
            volume=100,
        )

    # Asks not ascending
    unordered_asks = (
        Level(price=2502.0, qty=10),
        Level(price=2501.0, qty=10),
    )
    with pytest.raises(ValueError, match="Asks must be ordered ascending by price"):
        Quote(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            exch_ts=now,
            recv_ts=now,
            bids=(),
            asks=unordered_asks,
            ltp=2500.0,
            volume=100,
        )

    # Crossed market: best bid > best ask
    crossed_bids = (Level(price=2505.0, qty=10),)
    crossed_asks = (Level(price=2500.0, qty=10),)
    with pytest.raises(ValueError, match="Crossed market"):
        Quote(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            exch_ts=now,
            recv_ts=now,
            bids=crossed_bids,
            asks=crossed_asks,
            ltp=2500.0,
            volume=100,
        )

    # Non-positive quantity in level
    zero_qty_bid = (Level(price=2500.0, qty=0),)
    with pytest.raises(ValueError, match="Bid level qty must be positive"):
        Quote(
            instrument=InstrumentId("NSE:EQ:RELIANCE"),
            exch_ts=now,
            recv_ts=now,
            bids=zero_qty_bid,
            asks=(),
            ltp=2500.0,
            volume=100,
        )


def test_bar_validation() -> None:
    t0 = datetime(2026, 10, 23, 9, 15, tzinfo=UTC)
    t1 = datetime(2026, 10, 23, 9, 20, tzinfo=UTC)
    bar = Bar(
        instrument=InstrumentId("NSE:EQ:INFY"),
        timeframe="5m",
        start_ts=t0,
        end_ts=t1,
        open=1500.0,
        high=1510.0,
        low=1495.0,
        close=1505.0,
        volume=25000,
        complete=True,
    )
    assert bar.complete is True

    # High less than low
    with pytest.raises(ValueError, match="low.*cannot exceed high"):
        Bar(
            instrument=InstrumentId("NSE:EQ:INFY"),
            timeframe="5m",
            start_ts=t0,
            end_ts=t1,
            open=1500.0,
            high=1490.0,
            low=1495.0,
            close=1505.0,
            volume=10,
        )

    # Open outside low-high
    with pytest.raises(ValueError, match="open.*outside"):
        Bar(
            instrument=InstrumentId("NSE:EQ:INFY"),
            timeframe="5m",
            start_ts=t0,
            end_ts=t1,
            open=1520.0,
            high=1510.0,
            low=1495.0,
            close=1505.0,
            volume=10,
        )

    # Close outside low-high
    with pytest.raises(ValueError, match="close.*outside"):
        Bar(
            instrument=InstrumentId("NSE:EQ:INFY"),
            timeframe="5m",
            start_ts=t0,
            end_ts=t1,
            open=1500.0,
            high=1510.0,
            low=1495.0,
            close=1490.0,
            volume=10,
        )

    # Start after end
    with pytest.raises(ValueError, match="start_ts.*cannot be after end_ts"):
        Bar(
            instrument=InstrumentId("NSE:EQ:INFY"),
            timeframe="5m",
            start_ts=t1,
            end_ts=t0,
            open=1500.0,
            high=1510.0,
            low=1495.0,
            close=1505.0,
            volume=10,
        )


def test_feed_status_validation() -> None:
    now = datetime.now(UTC)
    s1 = FeedStatus(state="connected", ts=now)
    assert s1.state == "connected"

    s2 = FeedStatus(
        state="gap",
        ts=now,
        gap_start=now - timedelta(seconds=60),
        gap_end=now,
        details="reconnected after 60s",
    )
    assert s2.gap_start is not None

    with pytest.raises(ValueError, match="Invalid feed state"):
        FeedStatus(state="unknown", ts=now)

    with pytest.raises(ValueError, match="gap_start.*cannot be after gap_end"):
        FeedStatus(
            state="gap",
            ts=now,
            gap_start=now,
            gap_end=now - timedelta(seconds=10),
        )


def test_dataclasses_frozen_immutability() -> None:
    lvl = Level(price=10.0, qty=100)
    with pytest.raises((AttributeError, TypeError)):
        lvl.price = 20.0  # type: ignore[misc]

    tick = Tick(
        instrument=InstrumentId("NSE:EQ:TCS"),
        exch_ts=datetime.now(UTC),
        recv_ts=datetime.now(UTC),
        ltp=3500.0,
        ltq=1,
        volume=10,
    )
    with pytest.raises((AttributeError, TypeError)):
        tick.ltp = 3600.0  # type: ignore[misc]


TO_PRICE_VECTORS = [
    (100.02, Decimal("0.05"), Decimal("100.00")),
    (100.03, Decimal("0.05"), Decimal("100.05")),
    (100.07, Decimal("0.05"), Decimal("100.05")),
    (100.08, Decimal("0.05"), Decimal("100.10")),
    (100.00, Decimal("0.05"), Decimal("100.00")),
    (2450.123, Decimal("0.01"), Decimal("2450.12")),
    (2450.126, Decimal("0.01"), Decimal("2450.13")),
    (500.10, Decimal("0.25"), Decimal("500.00")),
    (500.15, Decimal("0.25"), Decimal("500.25")),
    (500.375, Decimal("0.25"), Decimal("500.50")),
    (1500.20, Decimal("0.50"), Decimal("1500.00")),
    (1500.30, Decimal("0.50"), Decimal("1500.50")),
    (105.4, Decimal("1.00"), Decimal("105.00")),
    (105.6, Decimal("1.00"), Decimal("106.00")),
    (725.0, Decimal("5.00"), Decimal("725.00")),
    (727.4, Decimal("5.00"), Decimal("725.00")),
    (727.6, Decimal("5.00"), Decimal("730.00")),
    (0.0, Decimal("0.05"), Decimal("0.00")),
]


@pytest.mark.parametrize("val,tick,expected", TO_PRICE_VECTORS)
def test_to_price_vectors(val: float, tick: Decimal, expected: Decimal) -> None:
    res = to_price(val, tick)
    assert res == expected


PARSE_ID_ROUNDTRIP_CASES = [
    ("NSE:EQ:INFY", "NSE", "EQ", "INFY", None, None, None),
    ("BSE:EQ:TCS", "BSE", "EQ", "TCS", None, None, None),
    ("MCX:COM:CRUDEOIL", "MCX", "COM", "CRUDEOIL", None, None, None),
    ("NSE:FUT:NIFTY:2026-10-29", "NSE", "FUT", "NIFTY", date(2026, 10, 29), None, None),
    ("NSE:FUT:BANKNIFTY:2026-11-26", "NSE", "FUT", "BANKNIFTY", date(2026, 11, 26), None, None),
    (
        "NSE:OPT:NIFTY:2026-10-29:25000:CE",
        "NSE",
        "OPT",
        "NIFTY",
        date(2026, 10, 29),
        Decimal("25000"),
        "CE",
    ),
    (
        "NSE:OPT:NIFTY:2026-10-29:24500:PE",
        "NSE",
        "OPT",
        "NIFTY",
        date(2026, 10, 29),
        Decimal("24500"),
        "PE",
    ),
    (
        "BSE:OPT:SENSEX:2026-10-30:80000.50:CE",
        "BSE",
        "OPT",
        "SENSEX",
        date(2026, 10, 30),
        Decimal("80000.50"),
        "CE",
    ),
]


@pytest.mark.parametrize("raw,exch,seg,sym,exp,strk,opt", PARSE_ID_ROUNDTRIP_CASES)
def test_parse_and_format_roundtrip(
    raw: str,
    exch: str,
    seg: str,
    sym: str,
    exp: date | None,
    strk: Decimal | None,
    opt: str | None,
) -> None:
    parts = parse_id(raw)
    assert parts.exchange == exch
    assert parts.segment == seg
    assert parts.symbol == sym
    assert parts.expiry == exp
    assert parts.strike == strk
    assert parts.option_type == opt
    assert format_id(parts) == raw


INVALID_ID_CASES = [
    ("", "Instrument id must be non-empty uppercase string"),
    ("NSE", "Expected 3, 4, or 6 colon-separated components"),
    ("NSE:EQ", "Expected 3, 4, or 6 colon-separated components"),
    ("nse:eq:reliance", "Instrument id must be non-empty uppercase string"),
    ("NSE:EQ:RELIANCE:EXTRA:PART:TOO:MANY", "Expected 3, 4, or 6 colon-separated components"),
    ("NSE:FUT:RELIANCE:2026-99-99", "Invalid expiry date"),
    ("NSE:OPT:NIFTY:2026-10-29:STRIKE:CE", "Invalid strike"),
    ("NSE:OPT:NIFTY:2026-10-29:25000:XX", "Option type must be 'CE' or 'PE'"),
]


@pytest.mark.parametrize("raw,err_pattern", INVALID_ID_CASES)
def test_parse_id_invalid_cases(raw: str, err_pattern: str) -> None:
    with pytest.raises(ValueError, match=err_pattern):
        parse_id(raw)
