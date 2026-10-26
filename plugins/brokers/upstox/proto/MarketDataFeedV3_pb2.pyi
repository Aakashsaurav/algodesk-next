"""Type stubs for the generated MarketDataFeedV3 protobuf module.

These stubs let static type checkers (mypy / Pyright) see the message
classes that ``BuildTopDescriptorsAndMessages`` injects at runtime.
"""

from __future__ import annotations

import enum
from typing import ClassVar, Mapping, MutableMapping

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf import wrappers_pb2
from google.protobuf.internal.containers import (
    RepeatedCompositeFieldContainer,
)

DESCRIPTOR: _descriptor.FileDescriptor

# ── enums ────────────────────────────────────────────────────────────

class Type(int, enum.IntEnum):
    initial_feed: ClassVar[Type]
    live_feed: ClassVar[Type]
    market_info: ClassVar[Type]

class RequestMode(int, enum.IntEnum):
    ltpc: ClassVar[RequestMode]
    full_d5: ClassVar[RequestMode]
    option_greeks: ClassVar[RequestMode]
    full_d30: ClassVar[RequestMode]

class MarketStatus(int, enum.IntEnum):
    PRE_OPEN_START: ClassVar[MarketStatus]
    PRE_OPEN_END: ClassVar[MarketStatus]
    NORMAL_OPEN: ClassVar[MarketStatus]
    NORMAL_CLOSE: ClassVar[MarketStatus]
    CLOSING_START: ClassVar[MarketStatus]
    CLOSING_END: ClassVar[MarketStatus]

# ── messages ─────────────────────────────────────────────────────────

class LTPC(_message.Message):
    ltp: float
    ltt: int
    ltq: int
    cp: float
    iep: wrappers_pb2.DoubleValue
    def __init__(
        self,
        *,
        ltp: float = ...,
        ltt: int = ...,
        ltq: int = ...,
        cp: float = ...,
        iep: wrappers_pb2.DoubleValue | None = ...,
    ) -> None: ...

class Quote(_message.Message):
    bidQ: int
    bidP: float
    askQ: int
    askP: float
    def __init__(
        self,
        *,
        bidQ: int = ...,
        bidP: float = ...,
        askQ: int = ...,
        askP: float = ...,
    ) -> None: ...

class MarketLevel(_message.Message):
    bidAskQuote: RepeatedCompositeFieldContainer[Quote]
    def __init__(
        self, *, bidAskQuote: list[Quote] | None = ...
    ) -> None: ...

class OHLC(_message.Message):
    interval: str
    open: float
    high: float
    low: float
    close: float
    vol: int
    ts: int
    def __init__(
        self,
        *,
        interval: str = ...,
        open: float = ...,
        high: float = ...,
        low: float = ...,
        close: float = ...,
        vol: int = ...,
        ts: int = ...,
    ) -> None: ...

class MarketOHLC(_message.Message):
    ohlc: RepeatedCompositeFieldContainer[OHLC]
    def __init__(self, *, ohlc: list[OHLC] | None = ...) -> None: ...

class OptionGreeks(_message.Message):
    delta: float
    theta: float
    gamma: float
    vega: float
    rho: float
    def __init__(
        self,
        *,
        delta: float = ...,
        theta: float = ...,
        gamma: float = ...,
        vega: float = ...,
        rho: float = ...,
    ) -> None: ...

class MarketFullFeed(_message.Message):
    ltpc: LTPC
    marketLevel: MarketLevel
    optionGreeks: OptionGreeks
    marketOHLC: MarketOHLC
    atp: float
    vtt: int
    oi: float
    iv: float
    tbq: float
    tsq: float
    iep: float
    rp: float
    ieq: int
    iiqTotal: int
    iiqM: int
    casEligible: bool
    def __init__(self, *, ltpc: LTPC | None = ..., marketLevel: MarketLevel | None = ..., optionGreeks: OptionGreeks | None = ..., marketOHLC: MarketOHLC | None = ..., atp: float = ..., vtt: int = ..., oi: float = ..., iv: float = ..., tbq: float = ..., tsq: float = ..., iep: float = ..., rp: float = ..., ieq: int = ..., iiqTotal: int = ..., iiqM: int = ..., casEligible: bool = ...) -> None: ...

class IndexFullFeed(_message.Message):
    ltpc: LTPC
    marketOHLC: MarketOHLC
    def __init__(
        self,
        *,
        ltpc: LTPC | None = ...,
        marketOHLC: MarketOHLC | None = ...,
    ) -> None: ...

class FullFeed(_message.Message):
    marketFF: MarketFullFeed
    indexFF: IndexFullFeed
    def __init__(
        self,
        *,
        marketFF: MarketFullFeed | None = ...,
        indexFF: IndexFullFeed | None = ...,
    ) -> None: ...

class FirstLevelWithGreeks(_message.Message):
    ltpc: LTPC
    firstDepth: Quote
    optionGreeks: OptionGreeks
    vtt: int
    oi: float
    iv: float
    def __init__(
        self,
        *,
        ltpc: LTPC | None = ...,
        firstDepth: Quote | None = ...,
        optionGreeks: OptionGreeks | None = ...,
        vtt: int = ...,
        oi: float = ...,
        iv: float = ...,
    ) -> None: ...

class Feed(_message.Message):
    ltpc: LTPC
    fullFeed: FullFeed
    firstLevelWithGreeks: FirstLevelWithGreeks
    requestMode: RequestMode
    def __init__(
        self,
        *,
        ltpc: LTPC | None = ...,
        fullFeed: FullFeed | None = ...,
        firstLevelWithGreeks: FirstLevelWithGreeks | None = ...,
        requestMode: RequestMode = ...,
    ) -> None: ...

class StatusInfo(_message.Message):
    status: str
    updatedTime: int
    def __init__(
        self, *, status: str = ..., updatedTime: int = ...
    ) -> None: ...

class MarketInfo(_message.Message):
    segmentStatus: MutableMapping[str, MarketStatus]
    casMarketStatus: MutableMapping[str, StatusInfo]
    preOpenSessionStatus: MutableMapping[str, StatusInfo]
    def __init__(
        self,
        *,
        segmentStatus: Mapping[str, MarketStatus] | None = ...,
        casMarketStatus: Mapping[str, StatusInfo] | None = ...,
        preOpenSessionStatus: Mapping[str, StatusInfo] | None = ...,
    ) -> None: ...

class FeedResponse(_message.Message):
    type: Type
    feeds: MutableMapping[str, Feed]
    currentTs: int
    marketInfo: MarketInfo
    def __init__(
        self,
        *,
        type: Type = ...,
        feeds: Mapping[str, Feed] | None = ...,
        currentTs: int = ...,
        marketInfo: MarketInfo | None = ...,
    ) -> None: ...
