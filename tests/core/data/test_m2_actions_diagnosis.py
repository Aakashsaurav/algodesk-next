"""Tests for CP1 readable corporate actions, Upstox diagnosis, and reconciler ratio fallback."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from plugins.brokers.upstox.config_schema import UpstoxConfig
from plugins.brokers.upstox.plugin import UpstoxBrokerPlugin
from typer.testing import CliRunner

from algodesk.api import ActionRecord, PluginContext, PluginManifest
from algodesk.api.errors import AuthError, DataError, RateLimitError
from algodesk.cli import app
from algodesk.data.actions_store import ActionsStore, CorporateActionsManager
from algodesk.data.reconciler import CorporateActionsReconciler


@pytest.fixture
def upstox_manifest() -> PluginManifest:
    return PluginManifest(
        name="upstox",
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:UpstoxBrokerPlugin",
        capabilities={
            "corporate_actions": True,
            "history": True,
            "stream": True,
        },
    )


@pytest.fixture
def fake_context() -> MagicMock:
    ctx = MagicMock(spec=PluginContext)
    ctx.params = MagicMock()
    ctx.params.get.return_value = None
    ctx.secrets = MagicMock()
    ctx.secrets.get.return_value = "fake_upstox_token_123"
    return ctx


def _make_plugin(manifest: PluginManifest, ctx: MagicMock) -> UpstoxBrokerPlugin:
    cfg = UpstoxConfig()
    return UpstoxBrokerPlugin(manifest, config=cfg.model_dump(), ctx=ctx)


# ---------------------------------------------------------------------------
# Requirement 6 & 7: Upstox typed error handling and unverified split/bonus
# ---------------------------------------------------------------------------


def test_upstox_reader_raises_typed_error_on_http_errors(
    upstox_manifest: PluginManifest, fake_context: MagicMock
) -> None:
    plugin = _make_plugin(upstox_manifest, fake_context)

    # 404 / 500 -> DataError
    for code in (404, 500, 502):
        mock_resp = MagicMock(status_code=code, json=MagicMock(return_value={"message": "fail"}))
        with patch.object(plugin._client(), "get", return_value=mock_resp):
            with pytest.raises(DataError) as exc_info:
                plugin.actions("RELIANCE")
            assert f"HTTP {code}" in str(exc_info.value.what)

    # 401 / 403 -> AuthError
    for code in (401, 403):
        mock_resp = MagicMock(status_code=code, json=MagicMock(return_value={"message": "auth"}))
        with patch.object(plugin._client(), "get", return_value=mock_resp):
            with pytest.raises(AuthError):
                plugin.actions("RELIANCE")

    # 429 -> RateLimitError
    mock_resp_429 = MagicMock(status_code=429, json=MagicMock(return_value={}))
    with patch.object(plugin._client(), "get", return_value=mock_resp_429):
        with pytest.raises(RateLimitError):
            plugin.actions("RELIANCE")


def test_upstox_reader_raises_typed_error_on_unexpected_shape(
    upstox_manifest: PluginManifest, fake_context: MagicMock
) -> None:
    plugin = _make_plugin(upstox_manifest, fake_context)

    # Malformed non-JSON response
    mock_resp_bad_json = MagicMock(status_code=200)
    mock_resp_bad_json.json.side_effect = ValueError("Invalid JSON")
    with patch.object(plugin._client(), "get", return_value=mock_resp_bad_json):
        with pytest.raises(DataError) as exc:
            plugin.actions("RELIANCE")
        assert "Invalid JSON" in exc.value.what

    # Payload is not a dict
    mock_resp_list = MagicMock(status_code=200, json=MagicMock(return_value=["unexpected"]))
    with patch.object(plugin._client(), "get", return_value=mock_resp_list):
        with pytest.raises(DataError) as exc:
            plugin.actions("RELIANCE")
        assert "expected dict" in exc.value.what

    # Payload missing events list
    mock_resp_no_events = MagicMock(
        status_code=200, json=MagicMock(return_value={"status": "success"})
    )
    with patch.object(plugin._client(), "get", return_value=mock_resp_no_events):
        with pytest.raises(DataError) as exc:
            plugin.actions("RELIANCE")
        assert "missing events list" in exc.value.what


def test_upstox_reader_marks_split_or_bonus_without_ratio_unverified(
    upstox_manifest: PluginManifest, fake_context: MagicMock
) -> None:
    plugin = _make_plugin(upstox_manifest, fake_context)
    mock_payload = {
        "status": "success",
        "data": [
            {
                "purpose": "Bonus Issue",
                "execution_date": "2024-10-28",
                "record_date": "2024-10-29",
                # Note: no ratio provided in this payload
            }
        ],
    }
    mock_resp = MagicMock(status_code=200, json=MagicMock(return_value=mock_payload))
    with patch.object(plugin._client(), "get", return_value=mock_resp):
        actions = plugin.actions("RELIANCE")

    assert len(actions) == 1
    act = actions[0]
    assert act.type == "bonus"
    assert act.ratio is None
    assert act.status == "unverified"
    assert "Missing ratio" in (act.reason or "")


def test_reconciler_takes_ratio_from_yfinance_when_upstox_lacks_ratio() -> None:
    upstox_bonus = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=None,
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
        reason="Missing ratio in Upstox response",
    )
    yfinance_bonus = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        ratio_old=1.0,
        ratio_new=2.0,
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )

    reconciler = CorporateActionsReconciler(primary_source="upstox")
    reconciled = reconciler.reconcile("NSE", "RELIANCE", [upstox_bonus, yfinance_bonus])

    assert len(reconciled) == 1
    rec = reconciled[0]
    assert rec.status == "confirmed"
    assert rec.ratio == 2.0
    assert rec.ratio_old == 1.0
    assert rec.ratio_new == 2.0
    assert rec.type == "bonus"


def test_upstox_probe_actions_raw(upstox_manifest: PluginManifest, fake_context: MagicMock) -> None:
    plugin = _make_plugin(upstox_manifest, fake_context)

    # Success probe
    mock_payload = {
        "status": "success",
        "data": [{"purpose": "Dividend", "amount": 10.0}],
    }
    mock_resp = MagicMock(status_code=200, json=MagicMock(return_value=mock_payload))
    with patch.object(plugin._client(), "get", return_value=mock_resp):
        diag = plugin.probe_actions_raw("RELIANCE")

    assert diag["symbol"] == "RELIANCE"
    assert diag["status_code"] == 200
    assert "status" in diag["top_level_keys"]
    assert "data" in diag["top_level_keys"]
    assert diag["item_count"] == 1
    assert diag["first_item"] == {"purpose": "Dividend", "amount": 10.0}
    # Ensure no token in string representation of diag
    assert "fake_upstox_token_123" not in str(diag)

    # Error probe
    mock_err_resp = MagicMock(
        status_code=404, json=MagicMock(return_value={"message": "Not Found"})
    )
    with patch.object(plugin._client(), "get", return_value=mock_err_resp):
        diag_err = plugin.probe_actions_raw("UNKNOWN")
    assert diag_err["status_code"] == 404
    assert diag_err["error"] == "Not Found"


# ---------------------------------------------------------------------------
# Requirement 5 & 6: CLI actions list, check, refresh, and spike upstox-actions
# ---------------------------------------------------------------------------


def test_cli_actions_refresh_prints_events_per_source(tmp_project: Path) -> None:
    class FakeSrc:
        def actions(self, sym: str):
            return [
                ActionRecord(
                    ex_date=date(2024, 10, 28),
                    type="bonus",
                    ratio=2.0,
                    source="upstox",
                    symbol=sym,
                    exchange="NSE",
                )
            ]

    data_dir = tmp_project / "data"
    store = ActionsStore(data_dir=data_dir)
    mgr = CorporateActionsManager(store=store, sources={"upstox": FakeSrc(), "yfinance": FakeSrc()})

    res = mgr.refresh("RELIANCE", force=True)
    assert res["actions_count"] == 2
    assert res["source_counts"]["upstox"] == 1
    assert res["source_counts"]["yfinance"] == 1


def test_cli_actions_list_columns_and_summary_counts(tmp_project: Path) -> None:
    data_dir = tmp_project / "data"
    store = ActionsStore(data_dir=data_dir)
    r1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
        reason="Upstox bonus",
    )
    r2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
        reason="YFinance bonus",
    )
    r3 = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("10.0"),
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
        reason="Single source feed",
    )
    store.write("NSE", "RELIANCE", [r1, r2, r3])

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "actions", "list", "RELIANCE"])
    assert res.exit_code == 0
    lines = res.stdout.splitlines()

    # Column header line check
    hdr = lines[0]
    for col in ["Date", "Type", "Ratio or Amount", "Sources", "Status", "Reason"]:
        assert col in hdr

    # Summary counts check
    assert "Summary counts:" in res.stdout
    assert "confirmed=1" in res.stdout
    assert "unverified=1" in res.stdout


def test_cli_actions_check_action_required_condition(tmp_project: Path) -> None:
    data_dir = tmp_project / "data"
    store = ActionsStore(data_dir=data_dir)
    runner = CliRunner()

    # Case A: Plain unverified only -> "Action required" must NOT appear
    r_unverified = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("10.0"),
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    store.write("NSE", "RELIANCE", [r_unverified])

    res_unv = runner.invoke(
        app, ["--root", str(tmp_project), "data", "actions", "check", "RELIANCE"]
    )
    assert res_unv.exit_code == 0
    assert "unverified" in res_unv.stdout
    assert "Summary counts: unverified=1" in res_unv.stdout
    assert "Action required" not in res_unv.stdout

    # Case B: Conflict or manual_required present on same date -> "Action required" MUST appear
    r1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
    )
    r2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
    )
    store.write("NSE", "RELIANCE", [r_unverified, r1, r2])

    res_conf = runner.invoke(
        app, ["--root", str(tmp_project), "data", "actions", "check", "RELIANCE"]
    )
    assert res_conf.exit_code == 0
    assert "conflict" in res_conf.stdout
    assert "Action required" in res_conf.stdout


def test_cli_actions_check_raw_flag(tmp_project: Path) -> None:
    data_dir = tmp_project / "data"
    store = ActionsStore(data_dir=data_dir)
    r1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0049,
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
        status="unverified",
    )
    store.write("NSE", "RELIANCE", [r1])

    runner = CliRunner()
    res = runner.invoke(
        app, ["--root", str(tmp_project), "data", "actions", "check", "--raw", "RELIANCE"]
    )
    assert res.exit_code == 0
    assert "RELIANCE" in res.stdout
    assert "unverified" in res.stdout


def test_cli_spike_upstox_actions_raw(tmp_project: Path) -> None:
    runner = CliRunner()
    diag_mock = {
        "symbol": "RELIANCE",
        "isin": "INE002A01018",
        "status_code": 200,
        "top_level_keys": ["status", "data"],
        "item_count": 1,
        "first_item": {"purpose": "Bonus", "ratio": "1:1"},
    }

    with patch("algodesk.cli.spike._open_rt") as mock_open:
        mock_rt = MagicMock()
        mock_open.return_value.__enter__.return_value = mock_rt
        mock_broker = MagicMock()
        mock_broker.probe_actions_raw.return_value = diag_mock
        mock_rt.plugins.create.return_value = mock_broker

        res = runner.invoke(
            app,
            [
                "--root",
                str(tmp_project),
                "spike",
                "upstox-actions",
                "--symbols",
                "RELIANCE",
                "--raw",
            ],
        )

    assert res.exit_code == 0
    assert "Symbol: RELIANCE" in res.stdout
    assert "ISIN: INE002A01018" in res.stdout
    assert "HTTP status: 200" in res.stdout
    assert "Top-level JSON keys: ['status', 'data']" in res.stdout
    assert "Item count: 1" in res.stdout
    assert "First item keys and values:" in res.stdout
