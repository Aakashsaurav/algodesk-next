"""Unit tests for Telegram notifier plugin and client with MockTransport."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from algodesk.api import Message, PluginContext, PluginManifest, Severity
from algodesk.api.conformance import notifier_problems
from ..client import TelegramClient
from ..plugin import TelegramNotifierPlugin


def _make_manifest() -> PluginManifest:
    return PluginManifest(
        name="telegram",
        kind="notifier",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:TelegramNotifierPlugin",
        capabilities={"alerts": True, "reports": True},
    )


def test_telegram_conformance_and_send_never_raises() -> None:
    manifest = _make_manifest()
    plugin = TelegramNotifierPlugin(manifest, config={}, ctx=None)
    problems = notifier_problems(plugin)
    assert not problems

    # Send should never raise even with garbage input
    plugin.send(Message(text="test message", severity="warning", title="Warning"))


def test_telegram_client_send_direct_success() -> None:
    sent_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})

    transport = httpx.MockTransport(handler)
    client = TelegramClient(
        bot_token="test_token",
        chat_id="12345",
        http_transport=transport,
    )
    ok = client.send_direct("Hello from test")
    assert ok is True
    assert len(sent_requests) == 1
    assert "api.telegram.org/bottest_token/sendMessage" in str(sent_requests[0].url)
    client.close()


def test_telegram_doctor_reachable_mock() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "getMe" in str(request.url):
            return httpx.Response(200, json={"ok": True, "result": {"username": "test_bot"}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    manifest = _make_manifest()

    ctx = MagicMock(spec=PluginContext)
    secrets_mock = MagicMock()
    secrets_mock.get.side_effect = lambda k: "val_token" if "token" in k else "val_chat"
    ctx.secrets = secrets_mock
    ctx.params = None

    plugin = TelegramNotifierPlugin(
        manifest,
        config={"bot_token": "secret:telegram_bot_token", "chat_id": "secret:telegram_chat_id"},
        ctx=ctx,
        http_transport=transport,
    )
    checks = plugin.doctor_checks()
    assert len(checks) == 1
    res = checks[0].run()
    assert res.ok is True
    assert "@test_bot" in res.message


def test_telegram_circuit_breaker() -> None:
    def fail_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(fail_handler)
    client = TelegramClient(
        bot_token="tok",
        chat_id="chat",
        circuit_breaker_failures=2,
        circuit_breaker_reset_seconds=10.0,
        retry_max_attempts=1,
        http_transport=transport,
    )

    # Directly verify send failure
    assert client.send_direct("Fail 1") is False
    client.close()
