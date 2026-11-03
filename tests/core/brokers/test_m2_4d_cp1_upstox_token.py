"""Tests for Upstox OAuth token resolution, secrets provider wiring, and raw diagnostic probe."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from plugins.brokers.upstox.config_schema import UpstoxConfig
from plugins.brokers.upstox.plugin import UpstoxBrokerPlugin
from typer.testing import CliRunner

from algodesk.api import PluginManifest, SecretsProvider
from algodesk.api.kinds import Mode
from algodesk.cli.app import app
from algodesk.cli.data import _build_action_sources
from algodesk.ops.context import build_context


class InMemoryFakeSecrets(SecretsProvider):
    """In-memory secrets provider mimicking keychain / env provider."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial or {})

    def get(self, name: str) -> str:
        if name not in self._store:
            raise KeyError(name)
        return self._store[name]

    def set(self, name: str, value: str) -> None:
        self._store[name] = value

    def names(self) -> Sequence[str]:
        return list(self._store.keys())


@pytest.fixture
def fake_upstox_manifest() -> PluginManifest:
    return PluginManifest(
        name="upstox",
        kind="broker",
        version="0.1.0",
        api_version=1,
        entry="plugin:UpstoxBrokerPlugin",
        capabilities={
            "orders": True,
            "positions": True,
            "history": True,
            "stream": True,
            "corporate_actions": True,
        },
        modes_allowed=["research", "backtest", "paper", "live"],
    )


def test_upstox_auth_flow_stores_token_and_reader_resolves_oauth(
    fake_upstox_manifest: PluginManifest,
) -> None:
    """Store token through UpstoxAuthFlow.complete path and verify reader resolves oauth token."""
    secrets = InMemoryFakeSecrets(
        {
            "upstox_client_id": "test_client_id",
            "upstox_client_secret": "test_client_secret",
            "upstox_redirect_uri": "http://127.0.0.1:8080/callback",
        }
    )
    ctx = build_context(Mode.RESEARCH, run_id="test_login", secrets=secrets)
    cfg = UpstoxConfig()
    broker = UpstoxBrokerPlugin(fake_upstox_manifest, cfg, ctx)

    # Initial state: no access token stored
    assert broker.resolve_active_token_type() == "none"
    assert broker.resolve_oauth_token() is None

    # Complete OAuth flow via mock HTTP exchange
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "mocked_oauth_access_token_xyz_999",
        "user_id": "U123456",
    }
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = None
    mock_client.post.return_value = mock_resp

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(broker._auth, "_client", lambda: mock_client)
        broker.complete("auth_code_12345")

    # Stored secret name must be 'upstox_access_token'
    assert "upstox_access_token" in secrets.names()
    assert secrets.get("upstox_access_token") == "mocked_oauth_access_token_xyz_999"

    # Reader must resolve active token type as 'oauth'
    assert broker.resolve_active_token_type() == "oauth"
    assert broker.resolve_oauth_token() == "mocked_oauth_access_token_xyz_999"


def test_probe_actions_raw_prints_names_never_values(
    fake_upstox_manifest: PluginManifest,
) -> None:
    """Raw probe returns token_type, secret names (never values), top-level keys, and item keys."""
    raw_token_value = "super_secret_raw_token_value_not_leaked"
    secrets = InMemoryFakeSecrets(
        {
            "upstox_access_token": raw_token_value,
            "upstox_api_key": "raw_api_key_hidden",
        }
    )
    ctx = build_context(Mode.RESEARCH, run_id="test_probe", secrets=secrets)
    cfg = UpstoxConfig()
    broker = UpstoxBrokerPlugin(fake_upstox_manifest, cfg, ctx)

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "success",
        "data": [
            {
                "event_type": "dividend",
                "record_date": "2024-10-28",
                "amount": 10.0,
            }
        ],
    }
    mock_client.get.return_value = mock_resp

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(broker, "_client", lambda: mock_client)
        diag = broker.probe_actions_raw("RELIANCE")

    assert diag["token_type"] == "oauth"
    assert diag["status_code"] == 200
    assert diag["secrets_present"] == ["upstox_access_token", "upstox_api_key"]
    assert "status" in diag["top_level_keys"]
    assert "data" in diag["top_level_keys"]
    assert "event_type" in diag["first_item_keys"]
    assert "amount" in diag["first_item_keys"]

    # Verify secret value is never in diagnostic output
    assert raw_token_value not in str(diag)
    assert "raw_api_key_hidden" not in str(diag)


def test_probe_actions_raw_on_http_error(
    fake_upstox_manifest: PluginManifest,
) -> None:
    """Probe handles HTTP 401 or errors cleanly returning secret names and HTTP status."""
    secrets = InMemoryFakeSecrets({"upstox_access_token": "expired_token"})
    ctx = build_context(Mode.RESEARCH, run_id="test_probe_err", secrets=secrets)
    cfg = UpstoxConfig()
    broker = UpstoxBrokerPlugin(fake_upstox_manifest, cfg, ctx)

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {"error": "Unauthorized", "message": "Invalid token"}
    mock_client.get.return_value = mock_resp

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(broker, "_client", lambda: mock_client)
        diag = broker.probe_actions_raw("INFY")

    assert diag["token_type"] == "oauth"
    assert diag["status_code"] == 401
    assert "upstox_access_token" in diag["secrets_present"]
    assert diag["top_level_keys"] == ["error", "message"]
    assert diag["error"] == "Invalid token"


def test_spike_upstox_actions_raw_cli_output(
    tmp_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI spike upstox-actions --raw prints token type, secret names, HTTP status, keys."""
    raw_token = "secret_cli_token_value_777"
    secrets = InMemoryFakeSecrets(
        {
            "upstox_access_token": raw_token,
            "upstox_api_key": "api_key_hidden_val",
        }
    )

    import algodesk.cli.spike as spike_mod

    monkeypatch.setattr(spike_mod, "_resolve_secrets_provider", lambda rt, **kw: secrets)

    # Mock client on created broker
    class DummyUpstox:
        def probe_actions_raw(self, symbol: str) -> dict[str, Any]:
            return {
                "symbol": symbol,
                "isin": f"INE_{symbol}",
                "token_type": "oauth",
                "secrets_present": ["upstox_access_token", "upstox_api_key"],
                "status_code": 200,
                "top_level_keys": ["status", "data"],
                "first_item_keys": ["event_type", "record_date"],
                "item_count": 1,
                "first_item": {"event_type": "dividend", "record_date": "2024-10-28"},
            }

    monkeypatch.setattr(
        spike_mod,
        "_open_rt",
        lambda ctx, **kw: MagicMock(
            __enter__=lambda self: MagicMock(
                plugins=MagicMock(create=lambda *a, **k: DummyUpstox()),
                root=tmp_project,
            ),
            __exit__=lambda *a: None,
        ),
    )

    runner = CliRunner()
    res = runner.invoke(app, ["spike", "upstox-actions", "--symbols", "RELIANCE", "--raw"])
    assert res.exit_code == 0
    assert "Token type: oauth" in res.stdout
    assert "Upstox secrets present: ['upstox_access_token', 'upstox_api_key']" in res.stdout
    assert "HTTP status: 200" in res.stdout
    assert "Top-level JSON keys: ['status', 'data']" in res.stdout
    assert "First item keys: ['event_type', 'record_date']" in res.stdout
    assert raw_token not in res.stdout


def test_build_action_sources_binds_secrets_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_action_sources creates broker plugins with secrets provider in context."""
    dummy_secrets = InMemoryFakeSecrets({"upstox_access_token": "tok123"})
    import algodesk.cli.data as data_mod

    monkeypatch.setattr(data_mod, "_resolve_secrets_provider", lambda rt, **kw: dummy_secrets)

    captured_ctx = {}

    def mock_create(kind: str, name: str, config: Any, ctx: Any) -> Any:
        captured_ctx[name] = ctx
        return MagicMock()

    mock_rt = MagicMock()
    mock_rt.plugins.create = mock_create

    sources = _build_action_sources(mock_rt, requested_source="upstox")
    assert "upstox" in sources
    assert captured_ctx["upstox"].secrets.get("upstox_access_token") == "tok123"


def test_probe_actions_raw_with_analytics_token(
    fake_upstox_manifest: PluginManifest,
) -> None:
    """When only analytics token is present, token_type is analytics and probe uses it."""
    secrets = InMemoryFakeSecrets({"upstox_analytics_token": "analytics_token_value_456"})
    ctx = build_context(Mode.RESEARCH, run_id="test_analytics_tok", secrets=secrets)
    cfg = UpstoxConfig()
    broker = UpstoxBrokerPlugin(fake_upstox_manifest, cfg, ctx)

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": []}
    mock_client.get.return_value = mock_resp

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(broker, "_client", lambda: mock_client)
        diag = broker.probe_actions_raw("TCS")

    assert diag["token_type"] == "analytics"
    assert "upstox_analytics_token" in diag["secrets_present"]
    # Check Authorization header was called with Bearer
    call_args = mock_client.get.call_args
    assert call_args[1]["headers"]["Authorization"] == "Bearer analytics_token_value_456"


def test_probe_actions_raw_without_token(
    fake_upstox_manifest: PluginManifest,
) -> None:
    """When no token is present, token_type is none and request sent without auth header."""
    secrets = InMemoryFakeSecrets({})
    ctx = build_context(Mode.RESEARCH, run_id="test_no_tok", secrets=secrets)
    cfg = UpstoxConfig()
    broker = UpstoxBrokerPlugin(fake_upstox_manifest, cfg, ctx)

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {"message": "Unauthorized"}
    mock_client.get.return_value = mock_resp

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(broker, "_client", lambda: mock_client)
        diag = broker.probe_actions_raw("WIPRO")

    assert diag["token_type"] == "none"
    assert diag["status_code"] == 401
    call_args = mock_client.get.call_args
    assert "Authorization" not in call_args[1]["headers"]


def test_spike_upstox_actions_report_generation(
    tmp_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When raw=False, spike upstox-actions generates markdown report and queries broker."""
    dummy_secrets = InMemoryFakeSecrets({"upstox_access_token": "tok_123"})
    import algodesk.cli.spike as spike_mod

    monkeypatch.setattr(spike_mod, "_resolve_secrets_provider", lambda rt, **kw: dummy_secrets)

    class DummyBroker:
        def actions(self, symbol: str) -> dict[str, Any]:
            return {"events": [{"event_type": "split"}]}

    monkeypatch.setattr(
        spike_mod,
        "_open_rt",
        lambda ctx, **kw: MagicMock(
            __enter__=lambda self: MagicMock(
                plugins=MagicMock(create=lambda *a, **k: DummyBroker()),
                root=tmp_project,
            ),
            __exit__=lambda *a: None,
        ),
    )

    runner = CliRunner()
    res = runner.invoke(app, ["spike", "upstox-actions", "--symbols", "RELIANCE"])
    assert res.exit_code == 0
    assert "Upstox Corporate Actions Spike" in res.stdout
    assert "RELIANCE" in res.stdout
    report_file = tmp_project / "_workbench" / "spike_upstox-actions.md"
    assert report_file.is_file()
