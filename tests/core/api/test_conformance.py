"""Tests for algodesk.api.conformance — pure conformance checkers for plugin kinds."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from algodesk.api.conformance import (
    calendar_problems,
    data_source_problems,
    indicator_problems,
    kind_problems,
    notifier_problems,
    risk_rule_problems,
    secrets_problems,
    strategy_problems,
    venue_problems,
)
from algodesk.api.errors import CalendarCoverageError, ConfigError
from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, Instrument, InstrumentId, Quote, SeriesKey, Tick
from algodesk.api.protocols import IndicatorSpec, Message, TradingSession
from algodesk.api.trading import (
    Fill,
    Funds,
    Intent,
    OrderSpec,
    Position,
    RiskDecision,
    VenueAck,
)


def test_data_source_problems_good_and_bad() -> None:
    class GoodSource:
        def instruments(self) -> list[Instrument]:
            return []

        def history(self, key: SeriesKey, start: datetime, end: datetime) -> list[Bar]:
            return []

        async def stream(self, instruments: Sequence[InstrumentId]) -> Any:
            yield None  # type: ignore[misc]

    assert data_source_problems(GoodSource()) == []

    class BadSource:
        def instruments(self) -> list[Instrument]:
            return []

    probs = data_source_problems(BadSource())
    assert any("history" in p for p in probs)
    assert any("stream" in p for p in probs)


def test_venue_problems_good_and_bad() -> None:
    class GoodVenue:
        def configure(self, instruments: Any) -> None:
            pass

        async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:
            return VenueAck(client_ref="r", venue_ref="v", ts=datetime.now(UTC))

        async def cancel(self, client_ref: str) -> VenueAck:
            return VenueAck(client_ref="r", venue_ref="v", ts=datetime.now(UTC))

        def order_status(self, client_ref: str) -> Any:
            return None

        def end_of_day(self) -> None:
            pass

        def on_quote(self, quote: Quote) -> None:
            pass

        async def events(self) -> Any:
            yield None  # type: ignore[misc]

        def positions(self) -> list[Position]:
            return []

        def funds(self) -> Funds:
            return Funds(available=Decimal("1000"))

    assert venue_problems(GoodVenue()) == []

    class SyncPlaceVenue:
        def configure(self, instruments: Any) -> None:
            pass

        def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:  # non-async!
            return VenueAck(client_ref="r", venue_ref="v", ts=datetime.now(UTC))

        async def cancel(self, client_ref: str) -> VenueAck:
            return VenueAck(client_ref="r", venue_ref="v", ts=datetime.now(UTC))

        def order_status(self, client_ref: str) -> Any:
            return None

        def end_of_day(self) -> None:
            pass

        def on_quote(self, quote: Quote) -> None:
            pass

        async def events(self) -> Any:
            yield None  # type: ignore[misc]

        def positions(self) -> list[Position]:
            return []

        def funds(self) -> Funds:
            return Funds(available=Decimal("1000"))

    probs = venue_problems(SyncPlaceVenue())
    assert any("async coroutine" in p for p in probs)


def test_indicator_problems_good_and_bad() -> None:
    class GoodIndicator:
        spec = IndicatorSpec(name="identity")

        def compute(self, values: Sequence[float]) -> Sequence[float | None]:
            return list(values)

        def new_state(self) -> object:
            # pyrefly: ignore[implicit-any-empty-container]
            return {}

        def update(self, state: object, value: float) -> float | None:
            return value

    assert indicator_problems(GoodIndicator()) == []
    golden = {"inputs": [1.0, 2.0, 3.0], "expected": [1.0, 2.0, 3.0]}
    assert indicator_problems(GoodIndicator(), golden=golden) == []

    # Batch and incremental mismatch
    class MismatchIndicator:
        spec = IndicatorSpec(name="bad")

        def compute(self, values: Sequence[float]) -> Sequence[float | None]:
            return [v * 2 for v in values]

        def new_state(self) -> object:
            # pyrefly: ignore[implicit-any-empty-container]
            return {}

        def update(self, state: object, value: float) -> float | None:
            return value

    probs = indicator_problems(MismatchIndicator())
    assert any("Numerical difference" in p for p in probs)

    # Missing spec
    class NoSpecIndicator:
        def compute(self, values: Sequence[float]) -> Sequence[float | None]:
            return list(values)

        def new_state(self) -> object:
            # pyrefly: ignore[implicit-any-empty-container]
            return {}

        def update(self, state: object, value: float) -> float | None:
            return value

    probs = indicator_problems(NoSpecIndicator())
    assert any("spec" in p for p in probs)


def test_strategy_problems_good_and_bad() -> None:
    class GoodStrategy:
        params_schema = dict

        def on_start(self, ctx: Any) -> None:
            pass

        def on_bar(self, ctx: Any, bar: Bar) -> list[Intent]:
            return []

        def on_tick(self, ctx: Any, tick: Tick) -> list[Intent]:
            return []

        def on_fill(self, ctx: Any, fill: Fill) -> list[Intent]:
            return []

        def on_timer(self, ctx: Any, name: str) -> list[Intent]:
            return []

        def on_stop(self, ctx: Any) -> None:
            pass

        def state(self) -> dict[str, object]:
            return {}

    assert strategy_problems(GoodStrategy()) == []

    class BadStrategy:
        pass

    probs = strategy_problems(BadStrategy())
    assert any("params_schema" in p for p in probs)
    assert any("on_bar" in p for p in probs)


def test_risk_rule_problems_good_and_bad() -> None:
    class GoodRule:
        def check(self, intent: Intent, view: Any) -> RiskDecision:
            return RiskDecision(allow=True, rule="ok")

    assert risk_rule_problems(GoodRule()) == []

    class BadRule:
        pass

    probs = risk_rule_problems(BadRule())
    assert any("check" in p for p in probs)


def test_notifier_problems_good_and_bad() -> None:
    class GoodNotifier:
        def send(self, message: Message) -> None:
            pass

    assert notifier_problems(GoodNotifier()) == []

    class RaisingNotifier:
        def send(self, message: Message) -> None:
            raise RuntimeError("Network disconnected")

    probs = notifier_problems(RaisingNotifier())
    assert any("raised an exception" in p for p in probs)


def test_calendar_problems_good_and_bad() -> None:
    class GoodCalendar:
        def is_trading_day(self, d: date) -> bool:
            if d.year < 2026:
                raise CalendarCoverageError(
                    what="Outside coverage", where="GoodCalendar", fix="Check date"
                )
            return True

        def sessions(self, d: date) -> list[TradingSession]:
            return []

        def next_session(self, ts: datetime) -> TradingSession:
            return TradingSession("regular", ts, ts)

    assert calendar_problems(GoodCalendar()) == []

    class BadCalendar:
        def is_trading_day(self, d: date) -> bool:
            return True  # Does not raise on outside date!

        def sessions(self, d: date) -> list[TradingSession]:
            return []

        def next_session(self, ts: datetime) -> TradingSession:
            return TradingSession("regular", ts, ts)

    probs = calendar_problems(BadCalendar())
    assert any("did not raise CalendarCoverageError" in p for p in probs)


def test_secrets_problems_good_and_bad() -> None:
    class GoodSecrets:
        def get(self, name: str) -> str:
            if name == "__algodesk_nonexistent_secret_key_conformance_check__":
                raise ConfigError(what="Key not found", where="vault", fix="Set key")
            return "secret_val"

        def set(self, name: str, value: str) -> None:
            pass

        def names(self) -> list[str]:
            return ["key1", "key2"]

    assert secrets_problems(GoodSecrets()) == []

    class BadSecrets:
        def get(self, name: str) -> str:
            return ""  # returns empty string instead of raising ConfigError!

        def set(self, name: str, value: str) -> None:
            pass

        def names(self) -> list[str]:
            return ["key1"]

    probs = secrets_problems(BadSecrets())
    assert any("did not raise ConfigError" in p for p in probs)


def test_kind_problems_dispatch() -> None:
    def _manifest(k: PluginKind) -> PluginManifest:
        return PluginManifest(
            name=f"test_{k.value}",
            kind=k,
            version="1.0.0",
            api_version=1,
            entry="plugin:Entry",
            capabilities={"run": True},
        )

    class ConformingNotifier:
        manifest = _manifest(PluginKind.NOTIFIER)

        def send(self, message: Message) -> None:
            pass

    assert kind_problems(ConformingNotifier()) == []

    class FailingNotifier:
        manifest = _manifest(PluginKind.NOTIFIER)

        def send(self, message: Message) -> None:
            raise ConnectionError("Failing transport")

    assert len(kind_problems(FailingNotifier())) > 0

    class NoManifestPlugin:
        pass

    assert any("manifest" in p for p in kind_problems(NoManifestPlugin()))


KINDS_TO_TEST = [
    PluginKind.BROKER,
    PluginKind.VENUE,
    PluginKind.INDICATOR,
    PluginKind.STRATEGY,
    PluginKind.RISK_RULE,
    PluginKind.NOTIFIER,
    PluginKind.CALENDAR,
    PluginKind.SECRETS,
    PluginKind.FEES,
]


@pytest.mark.parametrize("kind", KINDS_TO_TEST)
def test_kind_problems_catches_empty_implementation(kind: PluginKind) -> None:
    """An object with only manifest but no methods fails kind conformance for every kind."""
    manifest = PluginManifest(
        name=f"empty_{kind.value}",
        kind=kind,
        version="1.0.0",
        api_version=1,
        entry="plugin:Entry",
        capabilities={"run": True},
    )

    class EmptyPlugin:
        manifest: PluginManifest

    obj = EmptyPlugin()
    obj.manifest = manifest
    probs = kind_problems(obj)
    assert len(probs) > 0
    assert any("must" in p.lower() or "callable" in p.lower() for p in probs)


def test_clock_problems_good_and_bad() -> None:
    from algodesk.api import clock_problems

    class GoodClock:
        def now(self) -> datetime:
            return datetime.now(UTC)

        def monotonic_ns(self) -> int:
            return 123456789

    assert clock_problems(GoodClock()) == []

    class BadClockNoMethods:
        pass

    probs = clock_problems(BadClockNoMethods())
    assert any("now" in p for p in probs)
    assert any("monotonic_ns" in p for p in probs)

    class BadClockNaiveNow:
        def now(self) -> datetime:
            return datetime(2026, 1, 1, 10, 0)  # naive

        def monotonic_ns(self) -> int:
            return 100

    probs2 = clock_problems(BadClockNaiveNow())
    assert any("timezone-aware" in p for p in probs2)

    class BadClockBadMono:
        def now(self) -> datetime:
            return datetime.now(UTC)

        def monotonic_ns(self) -> str:
            return "not-an-int"  # type: ignore[return-value]

    probs3 = clock_problems(BadClockBadMono())
    assert any("monotonic_ns" in p for p in probs3)


def test_exchange_calendar_tradable_sessions_flag() -> None:
    from plugins.calendars.exchange.plugin import ExchangeCalendarPlugin

    manifest = PluginManifest(
        name="exchange",
        kind=PluginKind.CALENDAR,
        version="0.1.0",
        api_version=1,
        entry="plugin:ExchangeCalendarPlugin",
        capabilities={"trading_days": True},
    )
    # Test NSE EQ
    cal_nse = ExchangeCalendarPlugin(manifest, {"exchange": "nse"}, None)
    d = date(2026, 10, 21)  # Regular trading day
    sessions = cal_nse.sessions(d, segment="eq")
    assert len(sessions) == 2
    preopen = next(s for s in sessions if s.name == "preopen")
    regular = next(s for s in sessions if s.name == "regular")
    assert preopen.tradable is False
    assert regular.tradable is True

    # Test MCX COM
    cal_mcx = ExchangeCalendarPlugin(manifest, {"exchange": "mcx"}, None)
    mcx_sessions = cal_mcx.sessions(d, segment="com")
    assert len(mcx_sessions) == 2
    morning = next(s for s in mcx_sessions if s.name == "morning")
    evening = next(s for s in mcx_sessions if s.name == "evening")
    assert morning.tradable is True
    assert evening.tradable is True
