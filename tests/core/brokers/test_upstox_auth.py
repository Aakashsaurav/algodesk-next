"""Tests for Upstox AuthFlow implementation."""

from __future__ import annotations

import threading
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from plugins.brokers.upstox.auth import UpstoxAuthFlow
from plugins.brokers.upstox.config_schema import UpstoxConfig

from algodesk.api import AlgoDeskError, AuthSession, ConfigError
from algodesk.api.kinds import Mode
from algodesk.ops.context import build_context


class DictSecretReader:
    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets = dict(secrets)

    def get(self, name: str) -> str:
        if name not in self.secrets:
            raise ConfigError(
                what=f"Secret '{name}' not found", where=f"secret:{name}", fix="Set secret"
            )
        return self.secrets[name]

    def set(self, name: str, value: str) -> None:
        self.secrets[name] = value

    def names(self) -> list[str]:
        return list(self.secrets.keys())


@pytest.fixture
def test_secrets() -> DictSecretReader:
    return DictSecretReader(
        {
            "upstox_client_id": "test_app_id_123",  # fake-secret
            "upstox_client_secret": "test_app_secret_456",  # fake-secret
            "upstox_analytics_token": "test_analytics_tok_789",  # fake-secret
        }
    )


def test_auth_begin_generates_manual_url(test_secrets: DictSecretReader) -> None:
    ctx = build_context(Mode.RESEARCH, secrets=test_secrets)
    cfg = UpstoxConfig()
    auth = UpstoxAuthFlow(cfg, ctx)

    req = auth.begin()
    assert req.url.startswith("https://api.upstox.com/v2/login/authorization/dialog?")
    parsed = urllib.parse.urlparse(req.url)
    params = urllib.parse.parse_qs(parsed.query)

    assert params["client_id"] == ["test_app_id_123"]
    assert params["redirect_uri"] == ["http://127.0.0.1:5000/"]
    assert params["response_type"] == ["code"]
    assert params["state"] == [req.state]
    assert len(req.state) == 32


def test_auth_complete_exchanges_token(test_secrets: DictSecretReader) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.upstox.com/v2/login/authorization/token"
        assert request.method == "POST"
        body = urllib.parse.parse_qs(request.content.decode("utf-8"))
        assert body["code"] == ["test_code_abc"]
        assert body["client_id"] == ["test_app_id_123"]
        assert body["client_secret"] == ["test_app_secret_456"]

        data = {
            "access_token": "upstox_access_token_mock_val",  # fake-secret
            "user_id": "UP1234",
            "user_name": "Test User",
        }
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(handler)
    ctx = build_context(Mode.RESEARCH, secrets=test_secrets)
    cfg = UpstoxConfig()
    auth = UpstoxAuthFlow(cfg, ctx, http_transport=transport)

    session = auth.complete("test_code_abc")
    assert session.token == "upstox_access_token_mock_val"
    assert session.user_id == "UP1234"
    assert test_secrets.get("upstox_access_token") == "upstox_access_token_mock_val"

    # Verify expiry is 03:30 AM IST of the following day
    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist)
    exp = session.expires_at
    assert exp is not None
    exp_ist = exp.astimezone(ist)
    assert exp_ist.time() == time(3, 30)
    assert exp_ist.date() == now_ist.date() + timedelta(days=1)


def test_auth_validate_cheap_call(test_secrets: DictSecretReader) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        auth_hdr = request.headers.get("Authorization", "")
        if "valid_tok" in auth_hdr:
            return httpx.Response(200, json={"status": "success", "data": {"user_id": "UP1"}})
        return httpx.Response(401, json={"status": "error", "message": "Unauthorized"})

    transport = httpx.MockTransport(handler)
    ctx = build_context(Mode.RESEARCH, secrets=test_secrets)
    auth = UpstoxAuthFlow(UpstoxConfig(), ctx, http_transport=transport)

    assert auth.validate(AuthSession(token="valid_tok")) is True
    assert auth.validate(AuthSession(token="bad_tok")) is False


def test_auth_use_token_analytics(test_secrets: DictSecretReader) -> None:
    ctx = build_context(Mode.RESEARCH, secrets=test_secrets)
    auth = UpstoxAuthFlow(UpstoxConfig(), ctx)

    exp_date = date(2027, 10, 23)
    session = auth.use_token("my_analytics_token", expires=exp_date)
    assert session.token == "my_analytics_token"
    assert session.expires_at is not None
    assert session.expires_at.date() == exp_date
    assert session.extra.get("auth_type") == "analytics"


def test_auth_begin_auto_with_simulated_browser(
    test_secrets: DictSecretReader, monkeypatch: Any
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "auto_tok_xyz", "user_id": "UP99"})

    transport = httpx.MockTransport(handler)
    ctx = build_context(Mode.RESEARCH, secrets=test_secrets)
    # Use ephemeral port to avoid conflict
    cfg = UpstoxConfig(redirect_uri="http://127.0.0.1:58432/")
    auth = UpstoxAuthFlow(cfg, ctx, http_transport=transport)

    # Monkeypatch webbrowser.open to simulate browser navigating to redirect URI
    def fake_browser_open(url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        state = params["state"][0]

        def client_request() -> None:
            time.sleep(0.05)
            callback_url = f"http://127.0.0.1:58432/?code=browser_code_789&state={state}"
            try:
                urllib.request.urlopen(callback_url, timeout=2.0)
            except Exception:
                pass

        threading.Thread(target=client_request, daemon=True).start()
        return True

    import time

    monkeypatch.setattr("webbrowser.open", fake_browser_open)

    session = auth.begin_auto(timeout_s=5.0)
    assert session.token == "auto_tok_xyz"
    assert session.user_id == "UP99"


def test_auth_begin_auto_wrong_state_rejected(
    test_secrets: DictSecretReader, monkeypatch: Any
) -> None:
    ctx = build_context(Mode.RESEARCH, secrets=test_secrets)
    cfg = UpstoxConfig(redirect_uri="http://127.0.0.1:58433/")
    auth = UpstoxAuthFlow(cfg, ctx)

    def fake_browser_open(url: str) -> bool:
        def client_request() -> None:
            time.sleep(0.05)
            callback_url = "http://127.0.0.1:58433/?code=browser_code_789&state=wrong_state_val"
            try:
                urllib.request.urlopen(callback_url, timeout=2.0)
            except Exception:
                pass

        threading.Thread(target=client_request, daemon=True).start()
        return True

    import time

    monkeypatch.setattr("webbrowser.open", fake_browser_open)

    with pytest.raises(AlgoDeskError) as exc_info:
        auth.begin_auto(timeout_s=5.0)
    assert "state parameter mismatch" in exc_info.value.what.lower()
