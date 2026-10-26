"""Tests for Upstox broker plugin, AuthFlow, instruments, history, and feed."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from algodesk.api import (
    AlgoDeskError,
    ConfigError,
    InstrumentId,
    PluginManifest,
    Quote,
    SeriesKey,
    Tick,
    data_source_problems,
)

from ..auth import UpstoxAuthFlow
from ..config_schema import UpstoxConfig
from ..feed import UpstoxWebSocketFeed
from ..history import UpstoxHistoryClient
from ..instruments import UpstoxInstrumentManager
from ..plugin import UpstoxBrokerPlugin

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def manifest() -> PluginManifest:
    return PluginManifest(
        name="upstox",
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:UpstoxBrokerPlugin",
        capabilities={
            "history": True,
            "stream": True,
            "realtime": True,
            "live_suitable": True,
            "official_api": True,
            "delay_seconds": 0,
        },
    )


@pytest.fixture
def fake_context() -> MagicMock:
    ctx = MagicMock()
    ctx.params.get.side_effect = lambda k, d=None: d
    # Fake secrets reader/writer
    secrets_store = {
        "upstox_client_id": "test_client_id",  # fake-secret
        "upstox_client_secret": "test_secret_val",  # fake-secret
        "upstox_analytics_token": "test_analytics_tok",  # fake-secret
        "upstox_access_token": "test_access_tok",  # fake-secret
    }
    ctx.secrets.get.side_effect = lambda k: secrets_store.get(k, "")
    ctx.secrets.set.side_effect = lambda k, v: secrets_store.update({k: v})
    return ctx


def test_auth_begin_manual(fake_context: MagicMock) -> None:
    cfg = UpstoxConfig(redirect_uri="http://127.0.0.1:5000/")
    auth = UpstoxAuthFlow(cfg, fake_context)
    req = auth.begin()
    assert "https://api.upstox.com/v2/login/authorization/dialog" in req.url
    assert "client_id=test_client_id" in req.url
    assert req.state != ""
    assert len(req.state) == 32


def test_auth_complete_token_exchange(fake_context: MagicMock) -> None:
    token_json = json.loads((FIXTURES_DIR / "auth_token_response.json").read_text())

    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.upstox.com/v2/login/authorization/token"
        assert request.method == "POST"
        return httpx.Response(200, json=token_json)

    transport = httpx.MockTransport(mock_handler)
    cfg = UpstoxConfig()
    auth = UpstoxAuthFlow(cfg, fake_context, http_transport=transport)

    session = auth.complete("fake_auth_code_123")
    assert session.token == "fake_oauth_token_xyz"
    assert session.user_id == "UP123456"
    assert session.expires_at is not None
    assert session.extra.get("auth_type") == "oauth"


def test_auth_use_token_and_validate(fake_context: MagicMock) -> None:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        auth_header = request.headers.get("Authorization", "")
        if "good_token" in auth_header:
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(401, json={"status": "error"})

    transport = httpx.MockTransport(mock_handler)
    cfg = UpstoxConfig()
    auth = UpstoxAuthFlow(cfg, fake_context, http_transport=transport)

    today = date(2026, 10, 23)
    session = auth.use_token("good_token", expires=today)
    assert session.token == "good_token"
    assert session.expires_at is not None
    assert auth.validate(session) is True

    bad_session = auth.use_token("bad_token")
    assert auth.validate(bad_session) is False


def test_auth_begin_auto_flow(fake_context: MagicMock) -> None:
    token_json = json.loads((FIXTURES_DIR / "auth_token_response.json").read_text())

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=token_json)

    transport = httpx.MockTransport(mock_handler)
    port = 5022
    cfg = UpstoxConfig(redirect_uri=f"http://127.0.0.1:{port}/")
    auth = UpstoxAuthFlow(cfg, fake_context, http_transport=transport)

    import threading
    import time

    def _simulate_browser() -> None:
        time.sleep(0.1)
        st = auth._pending_state or "test_state"
        url = f"http://127.0.0.1:{port}/?code=test_code_abc&state={st}"
        with httpx.Client() as client:
            resp = client.get(url)
            assert resp.status_code == 200

    t = threading.Thread(target=_simulate_browser, daemon=True)
    t.start()

    session = auth.begin_auto(timeout_s=5.0)
    assert session.token == "fake_oauth_token_xyz"


def test_auth_begin_auto_state_mismatch(fake_context: MagicMock) -> None:
    port = 5023
    cfg = UpstoxConfig(redirect_uri=f"http://127.0.0.1:{port}/")
    auth = UpstoxAuthFlow(cfg, fake_context)

    import threading
    import time

    def _simulate_browser_bad_state() -> None:
        time.sleep(0.1)
        url = f"http://127.0.0.1:{port}/?code=test_code_abc&state=wrong_tampered_state"
        with httpx.Client() as client:
            client.get(url)

    t = threading.Thread(target=_simulate_browser_bad_state, daemon=True)
    t.start()

    with pytest.raises(AlgoDeskError, match="state parameter mismatch"):
        auth.begin_auto(timeout_s=5.0)


def test_instruments_mapping_and_resolve(tmp_path: Path, fake_context: MagicMock) -> None:
    mgr = UpstoxInstrumentManager(fake_context, cache_dir=tmp_path)
    sample_json = (FIXTURES_DIR / "instruments_sample.json").read_bytes()

    insts = mgr.load_or_fetch(raw_bytes=sample_json)
    assert len(insts) == 2

    # Canonical InstrumentId lookup
    rel = mgr.resolve(InstrumentId("NSE:EQ:RELIANCE"))
    assert rel.symbol == "RELIANCE"
    assert rel.lot_size == 1
    assert rel.broker_keys["upstox"] == "NSE_EQ|INE002A01018"

    # Bare symbol lookup
    nifty = mgr.resolve("Nifty 50")
    assert nifty.lot_size == 25
    assert nifty.broker_keys["upstox"] == "NSE_INDEX|Nifty 50"

    # Raw broker key lookup
    rel2 = mgr.resolve("NSE_EQ|INE002A01018")
    assert rel2.id == InstrumentId("NSE:EQ:RELIANCE")

    # Hard error on unknown symbol
    with pytest.raises(ConfigError, match="Cannot map unknown symbol"):
        mgr.resolve("UNKNOWN_XYZ")


def test_history_client(tmp_path: Path, fake_context: MagicMock) -> None:
    mgr = UpstoxInstrumentManager(fake_context, cache_dir=tmp_path)
    mgr.load_or_fetch(raw_bytes=(FIXTURES_DIR / "instruments_sample.json").read_bytes())

    candles_json = json.loads((FIXTURES_DIR / "candles_1d_response.json").read_text())

    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert "NSE_EQ|INE002A01018" in str(request.url)
        return httpx.Response(200, json=candles_json)

    transport = httpx.MockTransport(mock_handler)
    client = UpstoxHistoryClient(mgr, fake_context, http_transport=transport)

    key = SeriesKey(
        instrument=InstrumentId("NSE:EQ:RELIANCE"),
        kind="bars",
        timeframe="1d",
        adjustment="raw",
        source="upstox",
    )
    bars = list(
        client.history(key, datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 10, 23, tzinfo=UTC))
    )
    assert len(bars) == 2
    assert bars[0].start_ts < bars[1].start_ts  # Sorted ascending
    assert bars[0].open == 2460.0
    assert bars[1].close == 2500.5


def test_feed_decode_frames(tmp_path: Path, fake_context: MagicMock) -> None:
    mgr = UpstoxInstrumentManager(fake_context, cache_dir=tmp_path)
    mgr.load_or_fetch(raw_bytes=(FIXTURES_DIR / "instruments_sample.json").read_bytes())

    feed = UpstoxWebSocketFeed(mgr, fake_context)

    # 1. Decode snapshot frame
    snap_bytes = (FIXTURES_DIR / "feed_snapshot.bin").read_bytes()
    events = feed._decode_frame(snap_bytes)
    assert len(events) == 2
    tick = next(e for e in events if isinstance(e, Tick))
    quote = next(e for e in events if isinstance(e, Quote))

    assert tick.instrument == InstrumentId("NSE:EQ:RELIANCE")
    assert tick.ltp == 2500.50
    assert tick.volume == 1250000

    assert quote.instrument == InstrumentId("NSE:EQ:RELIANCE")
    assert len(quote.bids) == 5
    assert len(quote.asks) == 5
    assert quote.bids[0].price == 2500.50
    assert quote.asks[0].price == 2501.00

    # 2. Staleness tracking
    assert feed.is_stale(InstrumentId("NSE:EQ:RELIANCE")) is False

    # 3. Decode live tick frame
    live_bytes = (FIXTURES_DIR / "feed_live_tick.bin").read_bytes()
    events_live = feed._decode_frame(live_bytes)
    assert len(events_live) == 1
    assert events_live[0].ltp == 2502.00  # type: ignore[union-attr]

    # 4. Malformed frame handling (does not crash, returns empty list)
    malformed_events = feed._decode_frame(b"corrupt_invalid_binary_payload")
    assert malformed_events == []


def test_feed_limits_check(tmp_path: Path, fake_context: MagicMock) -> None:
    mgr = UpstoxInstrumentManager(fake_context, cache_dir=tmp_path)
    mgr.load_or_fetch(raw_bytes=(FIXTURES_DIR / "instruments_sample.json").read_bytes())
    feed = UpstoxWebSocketFeed(mgr, fake_context)

    # Exceeding keys limit
    huge_keys = [f"KEY_{i}" for i in range(2005)]
    with pytest.raises(AlgoDeskError, match="exceeds limit"):
        feed._check_limits(huge_keys)


def test_upstox_plugin_conformance(manifest: PluginManifest, fake_context: MagicMock) -> None:
    plugin = UpstoxBrokerPlugin(manifest, ctx=fake_context)
    problems = data_source_problems(plugin)
    assert problems == []
    # Check AuthFlow methods
    assert callable(getattr(plugin, "begin", None))
    assert callable(getattr(plugin, "begin_auto", None))
    assert callable(getattr(plugin, "complete", None))
    assert callable(getattr(plugin, "use_token", None))
    assert callable(getattr(plugin, "validate", None))
    assert callable(getattr(plugin, "expires_at", None))
