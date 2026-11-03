"""Tests for CP1: Upstox corporate actions OAuth authentication and diagnosis."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from plugins.brokers.upstox.plugin import AuthRequired, UpstoxBrokerPlugin
from typer.testing import CliRunner

from algodesk.api.manifest import PluginManifest
from algodesk.api.market import ActionRecord
from algodesk.api.services import Mode, PluginContext
from algodesk.cli.app import app
from algodesk.data.actions_store import ActionsStore, CorporateActionsManager


class DummySecrets:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = secrets or {}

    def get(self, name: str) -> str:
        if name in self._secrets:
            return self._secrets[name]
        raise KeyError(name)


class DummyParams:
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self._params = params or {}

    def get(self, name: str, scope: str | None = None) -> Any:
        return self._params.get(name)


def _make_manifest() -> PluginManifest:
    return PluginManifest(
        name="upstox",
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:UpstoxBrokerPlugin",
        capabilities={
            "history": True,
            "corporate_actions": True,
            "stream": True,
            "realtime": True,
            "live_suitable": True,
            "official_api": True,
            "delay_seconds": 0,
        },
    )


def test_upstox_actions_raises_auth_required_without_oauth_token(tmp_path: Path) -> None:
    manifest = _make_manifest()
    ctx = PluginContext(
        mode=Mode.RESEARCH,
        run_id="test_run",
        params=DummyParams(),  # type: ignore[arg-type]
        journal=MagicMock(),
        secrets=DummySecrets(),  # type: ignore[arg-type]
    )
    plugin = UpstoxBrokerPlugin(manifest=manifest, ctx=ctx, _cache_dir=tmp_path)
    with pytest.raises(AuthRequired) as exc_info:
        plugin.actions("RELIANCE")
    assert "run algodesk login upstox" in str(exc_info.value)


def test_upstox_actions_uses_oauth_token_when_valid(tmp_path: Path) -> None:
    manifest = _make_manifest()
    secret_token = "upstox_oauth_secret_abc123"
    ctx = PluginContext(
        mode=Mode.RESEARCH,
        run_id="test_run",
        params=DummyParams(),  # type: ignore[arg-type]
        journal=MagicMock(),
        secrets=DummySecrets({"upstox_access_token": secret_token}),  # type: ignore[arg-type]
    )

    captured_headers: dict[str, str] = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": [
                    {
                        "name": "Dividend",
                        "type": "dividend",
                        "amount": 10.0,
                        "ex_date": "2025-05-10",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(mock_handler)
    plugin = UpstoxBrokerPlugin(
        manifest=manifest,
        ctx=ctx,
        _http_transport=transport,
        _cache_dir=tmp_path,
    )
    # mock validate
    plugin.auth.validate = lambda sess: True  # type: ignore[assignment]

    acts = plugin.actions("RELIANCE")
    assert len(acts) == 1
    assert captured_headers.get("authorization") == f"Bearer {secret_token}"


def test_upstox_actions_raises_auth_required_on_401_or_403(tmp_path: Path) -> None:
    manifest = _make_manifest()
    ctx = PluginContext(
        mode=Mode.RESEARCH,
        run_id="test_run",
        params=DummyParams(),  # type: ignore[arg-type]
        journal=MagicMock(),
        secrets=DummySecrets({"upstox_access_token": "expired_tok"}),  # type: ignore[arg-type]
    )

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    transport = httpx.MockTransport(mock_handler)
    plugin = UpstoxBrokerPlugin(
        manifest=manifest,
        ctx=ctx,
        _http_transport=transport,
        _cache_dir=tmp_path,
    )
    plugin.auth.validate = lambda sess: True  # type: ignore[assignment]

    with pytest.raises(AuthRequired) as exc_info:
        plugin.actions("RELIANCE")
    assert "run algodesk login upstox" in str(exc_info.value)


def test_actions_refresh_continues_with_other_sources_on_auth_required(tmp_path: Path) -> None:
    manifest = MagicMock()
    manifest.get_dataset.return_value = None
    store = ActionsStore(data_dir=tmp_path / "data", manifest=manifest)

    # Source 1: upstox fails with AuthRequired
    upstox_src = MagicMock()
    upstox_src.actions.side_effect = AuthRequired(
        what="OAuth missing",
        where="UpstoxBrokerPlugin.actions",
        fix="run algodesk login upstox",
    )

    # Source 2: yfinance succeeds
    yf_act = ActionRecord(
        ex_date=date(2025, 4, 1),
        type="dividend",
        amount=Decimal("5.0"),
        symbol="TCS",
        exchange="NSE",
        status="unverified",
        source="yfinance",
    )
    yf_src = MagicMock()
    yf_src.actions.return_value = [yf_act]

    sources = {"upstox": upstox_src, "yfinance": yf_src}
    mgr = CorporateActionsManager(
        store=store,
        sources=sources,
        params=DummyParams({"data.actions_ttl_hours": 24}),  # type: ignore[arg-type]
    )

    res = mgr.refresh("TCS", force=True)
    assert "NSE:TCS" in res["refreshed"]
    assert res["actions_count"] == 1
    assert res["source_counts"]["yfinance"] == 1
    assert any("run algodesk login upstox" in w for w in res["warnings"])


def test_actions_refresh_raises_when_only_upstox_and_unauthorized(tmp_path: Path) -> None:
    manifest = MagicMock()
    manifest.get_dataset.return_value = None
    store = ActionsStore(data_dir=tmp_path / "data", manifest=manifest)

    upstox_src = MagicMock()
    upstox_src.actions.side_effect = AuthRequired(
        what="OAuth missing",
        where="UpstoxBrokerPlugin.actions",
        fix="run algodesk login upstox",
    )

    mgr = CorporateActionsManager(
        store=store,
        sources={"upstox": upstox_src},
        params=DummyParams({"data.actions_ttl_hours": 24}),  # type: ignore[arg-type]
    )
    with pytest.raises(AuthRequired) as exc_info:
        mgr.refresh("TCS", source="upstox", force=True)
    assert "run algodesk login upstox" in str(exc_info.value)


def test_spike_upstox_actions_raw_prints_token_type_name_never_value(
    monkeypatch: Any,
    tmp_project: Path,
) -> None:
    secret_val = "SECRET_SUPER_CONFIDENTIAL_TOKEN_XYZ"
    runner = CliRunner()

    from algodesk.cli.spike import _print_raw_diagnosis

    fake_broker = MagicMock()
    fake_broker.probe_actions_raw.return_value = {
        "symbol": "RELIANCE",
        "isin": "INE002A01018",
        "token_type": "oauth",
        "status_code": 200,
        "top_level_keys": ["data"],
        "item_count": 0,
        "first_item": {},
    }

    runner.invoke(
        app,
        ["--root", str(tmp_project), "spike", "upstox-actions", "--raw", "--symbols", "RELIANCE"],
    )
    # If invoked directly via cli, test the helper function explicitly
    output_lines: list[str] = []
    monkeypatch.setattr("typer.echo", lambda msg="": output_lines.append(str(msg)))
    _print_raw_diagnosis(fake_broker, "RELIANCE")
    full_output = "\n".join(output_lines)

    assert "Token type: oauth" in full_output
    assert secret_val not in full_output


def test_doctor_warns_when_actions_stale_and_oauth_missing(tmp_path: Path) -> None:
    manifest = _make_manifest()
    data_dir = tmp_path / "data"
    act_file = data_dir / "actions" / "exchange=NSE" / "symbol=RELIANCE" / "actions.parquet"
    act_file.parent.mkdir(parents=True, exist_ok=True)
    act_file.write_bytes(b"dummy")
    # Make file 48 hours old
    stale_time = (datetime.now(UTC) - timedelta(hours=48)).timestamp()
    os.utime(act_file, (stale_time, stale_time))

    ctx = PluginContext(
        mode=Mode.RESEARCH,
        run_id="test_run",
        params=DummyParams({"data.actions_ttl_hours": 24}),  # type: ignore[arg-type]
        journal=MagicMock(),
        secrets=DummySecrets(),  # type: ignore[arg-type]
    )
    plugin = UpstoxBrokerPlugin(
        manifest=manifest,
        ctx=ctx,
        _cache_dir=tmp_path,
        _data_dir=data_dir,
    )
    res = plugin._check_actions_oauth()
    assert res.ok is False
    assert "older than data.actions_ttl_hours" in res.message
    assert "run algodesk login upstox" in str(res.fix)


def test_doctor_passes_when_actions_fresh_even_if_oauth_missing(tmp_path: Path) -> None:
    manifest = _make_manifest()
    data_dir = tmp_path / "data"
    act_file = data_dir / "actions" / "exchange=NSE" / "symbol=RELIANCE" / "actions.parquet"
    act_file.parent.mkdir(parents=True, exist_ok=True)
    act_file.write_bytes(b"dummy")
    # Fresh file (now)
    now_time = datetime.now(UTC).timestamp()
    os.utime(act_file, (now_time, now_time))

    ctx = PluginContext(
        mode=Mode.RESEARCH,
        run_id="test_run",
        params=DummyParams({"data.actions_ttl_hours": 24}),  # type: ignore[arg-type]
        journal=MagicMock(),
        secrets=DummySecrets(),  # type: ignore[arg-type]
    )
    plugin = UpstoxBrokerPlugin(
        manifest=manifest,
        ctx=ctx,
        _cache_dir=tmp_path,
        _data_dir=data_dir,
    )
    res = plugin._check_actions_oauth()
    assert res.ok is True


def test_doctor_passes_when_oauth_valid_even_if_actions_stale(tmp_path: Path) -> None:
    manifest = _make_manifest()
    data_dir = tmp_path / "data"
    act_file = data_dir / "actions" / "exchange=NSE" / "symbol=RELIANCE" / "actions.parquet"
    act_file.parent.mkdir(parents=True, exist_ok=True)
    act_file.write_bytes(b"dummy")
    stale_time = (datetime.now(UTC) - timedelta(hours=48)).timestamp()
    os.utime(act_file, (stale_time, stale_time))

    ctx = PluginContext(
        mode=Mode.RESEARCH,
        run_id="test_run",
        params=DummyParams({"data.actions_ttl_hours": 24}),  # type: ignore[arg-type]
        journal=MagicMock(),
        secrets=DummySecrets({"upstox_access_token": "valid_token"}),  # type: ignore[arg-type]
    )
    plugin = UpstoxBrokerPlugin(
        manifest=manifest,
        ctx=ctx,
        _cache_dir=tmp_path,
        _data_dir=data_dir,
    )
    plugin.auth.validate = lambda sess: True  # type: ignore[assignment]
    res = plugin._check_actions_oauth()
    assert res.ok is True


def test_doctor_passes_when_no_actions_cache_exists(tmp_path: Path) -> None:
    manifest = _make_manifest()
    data_dir = tmp_path / "data"

    ctx = PluginContext(
        mode=Mode.RESEARCH,
        run_id="test_run",
        params=DummyParams({"data.actions_ttl_hours": 24}),  # type: ignore[arg-type]
        journal=MagicMock(),
        secrets=DummySecrets(),  # type: ignore[arg-type]
    )
    plugin = UpstoxBrokerPlugin(
        manifest=manifest,
        ctx=ctx,
        _cache_dir=tmp_path,
        _data_dir=data_dir,
    )
    res = plugin._check_actions_oauth()
    assert res.ok is True


def test_probe_actions_raw_reports_none_token_type(tmp_path: Path) -> None:
    manifest = _make_manifest()
    ctx = PluginContext(
        mode=Mode.RESEARCH,
        run_id="test_run",
        params=DummyParams(),  # type: ignore[arg-type]
        journal=MagicMock(),
        secrets=DummySecrets(),  # type: ignore[arg-type]
    )

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": "error", "message": "Unauthorized"})

    transport = httpx.MockTransport(mock_handler)
    plugin = UpstoxBrokerPlugin(
        manifest=manifest,
        ctx=ctx,
        _http_transport=transport,
        _cache_dir=tmp_path,
    )
    plugin._instruments_mgr.resolve_isin = lambda s: "INE002A01018"  # type: ignore[assignment]
    res = plugin.probe_actions_raw("RELIANCE")
    assert res["token_type"] == "none"
    assert "access_token" not in res
    assert res["status_code"] == 401


def test_refresh_multiple_sources_one_fails(tmp_path: Path) -> None:
    manifest = MagicMock()
    manifest.get_dataset.return_value = None
    store = ActionsStore(data_dir=tmp_path / "data", manifest=manifest)
    act = ActionRecord(
        ex_date=date(2025, 1, 1),
        type="dividend",
        amount=Decimal("5.0"),
        symbol="RELIANCE",
        exchange="NSE",
        status="verified",
    )
    b1 = MagicMock()
    b1.actions.side_effect = AuthRequired(
        what="OAuth missing",
        where="upstox",
        fix="run algodesk login upstox",
    )
    b2 = MagicMock()
    b2.actions.return_value = [act]

    mgr = CorporateActionsManager(
        store=store,
        sources={"upstox": b1, "mock_source": b2},
        params=DummyParams({"data.actions_ttl_hours": 24}),  # type: ignore[arg-type]
    )
    res = mgr.refresh("RELIANCE", force=True)
    assert res["actions_count"] == 1
    assert any("run algodesk login upstox" in w for w in res["warnings"])
