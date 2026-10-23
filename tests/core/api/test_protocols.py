"""Tests for algodesk.api.protocols — protocol definitions and supporting types."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from algodesk.api.market import (
    Bar,
    Instrument,
    Quote,
    SeriesKey,
    Tick,
)
from algodesk.api.protocols import (
    AuthFlow,
    AuthSession,
    Calendar,
    DataSource,
    ExecutionVenue,
    Indicator,
    IndicatorSpec,
    LoginRequest,
    Message,
    Notifier,
    RiskRule,
    RiskView,
    SecretsProvider,
    Session,
    Strategy,
    StrategyContext,
    TradingSession,
)
from algodesk.api.trading import (
    Fill,
    Funds,
    Intent,
    OrderSpec,
    Position,
    RiskDecision,
    VenueAck,
)


def test_supporting_types_instantiation() -> None:
    now = datetime.now(UTC)

    req = LoginRequest(url="https://login.example.com", state="xyz123", instructions="Paste code")
    assert req.url == "https://login.example.com"

    auth = AuthSession(token="secret_token", expires_at=now, user_id="user_1")
    assert auth.token == "secret_token"

    spec = IndicatorSpec(
        name="sma", inputs=("close",), params_schema={"period": 20}, outputs=("value",), warmup=20
    )
    assert spec.name == "sma"
    assert spec.warmup == 20

    tsess = TradingSession(name="regular", start=now, end=now)
    assert tsess.name == "regular"
    assert tsess.open == now
    assert tsess.close == now
    assert tsess == Session(name="regular", start=now, end=now)

    ctx = StrategyContext(params={"fast": 10})
    assert ctx.params["fast"] == 10

    rv = RiskView(daily_pnl=Decimal("150.00"))
    assert rv.daily_pnl == Decimal("150.00")

    msg = Message(text="System online", severity="INFO", title="Status")
    assert msg.text == "System online"


def test_auth_flow_protocol_runtime_check() -> None:
    class FakeAuth:
        def begin(self) -> LoginRequest:
            return LoginRequest(url="https://test")

        def begin_auto(self, *, timeout_s: float) -> AuthSession:
            return AuthSession(token="tok")

        def complete(self, code: str) -> AuthSession:
            return AuthSession(token="tok")

        def use_token(self, token: str, expires: date | None) -> AuthSession:
            return AuthSession(token="tok")

        def validate(self, session: AuthSession) -> bool:
            return True

        def expires_at(self, session: AuthSession) -> datetime | None:
            return None

    assert isinstance(FakeAuth(), AuthFlow)


def test_data_source_protocol_runtime_check() -> None:
    class FakeSource:
        def instruments(self) -> list[Instrument]:
            return []

        def history(self, key: SeriesKey, start: datetime, end: datetime) -> list[Bar]:
            return []

        async def stream(self, instruments: Any) -> Any:
            yield None  # type: ignore[misc]

    assert isinstance(FakeSource(), DataSource)


def test_execution_venue_protocol_runtime_check() -> None:
    class FakeVenue:
        async def place(self, spec: OrderSpec, client_ref: str) -> VenueAck:
            return VenueAck(client_ref=client_ref, venue_ref="v", ts=datetime.now(UTC))

        async def cancel(self, client_ref: str) -> VenueAck:
            return VenueAck(client_ref=client_ref, venue_ref="v", ts=datetime.now(UTC))

        def on_quote(self, quote: Quote) -> None:
            pass

        async def events(self) -> Any:
            yield None  # type: ignore[misc]

        def positions(self) -> list[Position]:
            return []

        def funds(self) -> Funds:
            return Funds(available=Decimal("1000"))

    assert isinstance(FakeVenue(), ExecutionVenue)


def test_indicator_protocol_runtime_check() -> None:
    class FakeIndicator:
        spec = IndicatorSpec(name="dummy")

        def compute(self, values: Any) -> list[float | None]:
            return [v for v in values]

        def new_state(self) -> object:
            return {}

        def update(self, state: object, value: float) -> float | None:
            return value

    assert isinstance(FakeIndicator(), Indicator)


def test_strategy_protocol_runtime_check() -> None:
    class FakeStrategy:
        params_schema = dict

        def on_start(self, ctx: StrategyContext) -> None:
            pass

        def on_bar(self, ctx: StrategyContext, bar: Bar) -> list[Intent]:
            return []

        def on_tick(self, ctx: StrategyContext, tick: Tick) -> list[Intent]:
            return []

        def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]:
            return []

        def on_timer(self, ctx: StrategyContext, name: str) -> list[Intent]:
            return []

        def on_stop(self, ctx: StrategyContext) -> None:
            pass

        def state(self) -> dict[str, object]:
            return {}

    assert isinstance(FakeStrategy(), Strategy)


def test_risk_rule_protocol_runtime_check() -> None:
    class FakeRiskRule:
        def check(self, intent: Intent, view: RiskView) -> RiskDecision:
            return RiskDecision(allow=True, rule="dummy")

    assert isinstance(FakeRiskRule(), RiskRule)


def test_notifier_protocol_runtime_check() -> None:
    class FakeNotifier:
        def send(self, message: Message) -> None:
            pass

    assert isinstance(FakeNotifier(), Notifier)


def test_calendar_protocol_runtime_check() -> None:
    class FakeCalendar:
        def is_trading_day(self, d: date) -> bool:
            return True

        def sessions(self, d: date) -> list[TradingSession]:
            return []

        def next_session(self, ts: datetime) -> TradingSession:
            return TradingSession("regular", ts, ts)

    assert isinstance(FakeCalendar(), Calendar)


def test_secrets_provider_protocol_runtime_check() -> None:
    class FakeSecrets:
        def get(self, name: str) -> str:
            return "val"

        def set(self, name: str, value: str) -> None:
            pass

        def names(self) -> list[str]:
            return ["key1"]

    assert isinstance(FakeSecrets(), SecretsProvider)
