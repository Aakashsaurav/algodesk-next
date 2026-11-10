"""Tests for Upstox feed HTTP 401 rejection and typed FeedAuthError."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from plugins.brokers.upstox.feed import UpstoxWebSocketFeed

from algodesk.api import AlgoDeskError, AuthError, FeedAuthError


def test_feed_auth_error_hierarchy() -> None:
    """FeedAuthError inherits from AuthError and AlgoDeskError."""
    err = FeedAuthError(
        what="WebSocket rejected: 401",
        where="UpstoxFeed",
        fix="run algodesk login upstox",
    )
    assert isinstance(err, AuthError)
    assert isinstance(err, AlgoDeskError)
    assert err.fix == "run algodesk login upstox"
    assert "401" in err.what


def test_upstox_feed_authorize_401_raises_feed_auth_error() -> None:
    """HTTP 401 in authorize_feed raises typed FeedAuthError."""
    inst_mgr = MagicMock()
    feed = UpstoxWebSocketFeed(instruments=inst_mgr, ctx=None)

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"

    with patch("httpx.Client.get", return_value=mock_resp):
        with pytest.raises(FeedAuthError) as exc_info:
            feed.authorize_feed("bad_token")

        assert "401" in exc_info.value.what
        assert exc_info.value.fix == "run algodesk login upstox"


def test_upstox_feed_stream_reauth_at_most_once_on_401() -> None:
    """Feed 401 during stream attempts re-auth at most once before raising FeedAuthError."""
    inst_mgr = MagicMock()
    feed = UpstoxWebSocketFeed(instruments=inst_mgr, ctx=None)
    feed._is_running = True

    reauth_count = 0

    def _mock_auth(token: str) -> str:
        nonlocal reauth_count
        reauth_count += 1
        raise FeedAuthError(
            what="Re-auth failed with HTTP 401",
            where="UpstoxWebSocketFeed.authorize_feed",
            fix="run algodesk login upstox",
        )

    feed.authorize_feed = _mock_auth  # type: ignore[assignment]

    async def _test() -> None:
        err_401 = Exception("HTTP 401 Unauthorized")
        with patch("websockets.connect", side_effect=err_401):
            with pytest.raises(FeedAuthError) as exc_info:
                async for _ in feed._stream_with_reconnect(
                    "wss://test", ["NSE_EQ|123"], token="tok"
                ):
                    pass
            assert "401" in exc_info.value.what
            assert exc_info.value.fix == "run algodesk login upstox"

    asyncio.run(_test())
    assert reauth_count == 1


def test_upstox_feed_stream_immediate_401_without_reauth_loop() -> None:
    """Feed connection rejection with 401 stops immediately with FeedAuthError without loop."""
    inst_mgr = MagicMock()
    feed = UpstoxWebSocketFeed(instruments=inst_mgr, ctx=None)
    feed._is_running = True

    connect_calls = 0

    class MockConnectCtx:
        async def __aenter__(self):
            nonlocal connect_calls
            connect_calls += 1
            raise Exception("server rejected WebSocket connection: HTTP 401")

        async def __aexit__(self, *args):
            pass

    async def _test() -> None:
        with patch("websockets.connect", return_value=MockConnectCtx()):
            feed.authorize_feed = MagicMock(return_value="wss://new_url")  # type: ignore[assignment]
            with pytest.raises(FeedAuthError):
                async for _ in feed._stream_with_reconnect(
                    "wss://initial", ["NSE_EQ|123"], token="tok"
                ):
                    pass

    asyncio.run(_test())
    assert connect_calls <= 2


def test_upstox_plugin_stream_missing_token_raises_feed_auth_error() -> None:
    """UpstoxBrokerPlugin.stream raises FeedAuthError if access token is empty."""
    from plugins.brokers.upstox.plugin import UpstoxBrokerPlugin

    manifest = MagicMock()
    manifest.order_capable = False
    manifest.modes_allowed = [MagicMock()]
    ctx = MagicMock()
    ctx.secrets = MagicMock()
    ctx.secrets.get = MagicMock(return_value=None)
    plugin = UpstoxBrokerPlugin(manifest=manifest, config={}, ctx=ctx)
    plugin.resolve_active_token = MagicMock(return_value=None)  # type: ignore[assignment]

    async def _run() -> None:
        with pytest.raises(FeedAuthError) as exc_info:
            async for _ in plugin.stream(["NSE:EQ:RELIANCE"]):
                pass
        assert "token" in exc_info.value.what.lower()
        assert exc_info.value.fix == "run algodesk login upstox"

    asyncio.run(_run())
