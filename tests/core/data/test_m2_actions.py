"""Tests for ActionRecord, ActionsStore, and broker readers (CP0)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from algodesk.api import ActionRecord


def test_action_record_creation_and_coercion():
    rec = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0049,
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
    )
    assert rec.ex_date == date(2024, 10, 28)
    assert rec.type == "bonus"
    assert rec.ratio == 1.0049
    assert rec.source == "upstox"
    assert rec.symbol == "RELIANCE"
    assert rec.exchange == "NSE"
    assert rec.status == "unverified"
    assert rec.id == "upstox:NSE:RELIANCE:2024-10-28:bonus"


def test_action_record_amount_decimal_coercion():
    rec = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=5.5,  # float coerced to Decimal
        source="yfinance",
        symbol="TCS",
        exchange="NSE",
    )
    assert isinstance(rec.amount, Decimal)
    assert rec.amount == Decimal("5.5")


def test_action_record_failure_on_invalid_date():
    with pytest.raises((ValueError, TypeError)):
        ActionRecord(
            ex_date="not-a-date",  # type: ignore
            type="dividend",
            symbol="INFY",
        )


def test_actions_store_write_read_and_manifest(tmp_path):
    from algodesk.data.actions_store import ActionsStore
    from algodesk.data.manifest import DataManifest

    manifest_db = tmp_path / "state" / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    store = ActionsStore(data_dir=tmp_path, manifest=manifest)

    rec1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0049,
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
        fetched_at="2026-10-31T10:00:00Z",
    )
    p = store.write("NSE", "RELIANCE", [rec1], source="upstox")
    assert p.exists()
    assert "actions/exchange=NSE/symbol=RELIANCE/actions.parquet" in str(p)

    # Read back
    actions = store.read("NSE", "RELIANCE")
    assert len(actions) == 1
    assert actions[0].ex_date == date(2024, 10, 28)
    assert actions[0].type == "bonus"
    assert actions[0].ratio == 1.0049
    assert actions[0].source == "upstox"

    # Verify manifest has it with kind="actions"
    files = manifest.get_files(kind="actions")
    assert len(files) == 1
    assert files[0].kind == "actions"
    assert files[0].rows == 1
    assert "actions.parquet" in files[0].path

    # Merge second source (yfinance)
    rec2 = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("10.0"),
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
        fetched_at="2026-10-31T10:05:00Z",
    )
    store.write("NSE", "RELIANCE", [rec2], source="yfinance")

    actions_merged = store.read("NSE", "RELIANCE")
    assert len(actions_merged) == 2
    sources = {a.source for a in actions_merged}
    assert sources == {"upstox", "yfinance"}


def test_upstox_actions_reader(tmp_path):

    import httpx
    from plugins.brokers.upstox.plugin import UpstoxBrokerPlugin

    from algodesk.api import PluginManifest

    upstox_fixture = {
        "status": "success",
        "data": [
            {
                "name": "Dividend",
                "expiry_date": "2024-08-14",
                "amount": 10.0,
                "event_details": [{"name": "Details", "value": "Rs.10.00 final dividend"}],
            },
            {
                "name": "Stock Split",
                "expiry_date": "2024-10-28",
                "amount": None,
                # Ratio missing in response -> must be marked unverified
            },
            {
                "name": "Capital Restructuring",
                "expiry_date": "2025-01-15",
                # Unknown type -> maps to other & manual_required
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=upstox_fixture)

    transport = httpx.MockTransport(handler)
    manifest = PluginManifest.model_validate(
        {
            "name": "upstox",
            "kind": "broker",
            "version": "0.1.0",
            "api_version": 1,
            "entry": "plugin:UpstoxBrokerPlugin",
            "capabilities": {"corporate_actions": True, "history": True},
        }
    )
    plugin = UpstoxBrokerPlugin(
        manifest=manifest,
        _http_transport=transport,
        _cache_dir=tmp_path / "cache",
    )
    plugin.use_token("fake_oauth_token")

    actions = plugin.actions("RELIANCE")
    assert len(actions) == 3
    # Dividend
    div = actions[0]
    assert div.type == "dividend"
    assert div.amount == Decimal("10.0")
    assert div.ex_date == date(2024, 8, 14)

    # Split without ratio -> unverified
    split = actions[1]
    assert split.type == "split"
    assert split.ratio is None
    assert split.status == "unverified"

    # Unknown type -> other, manual_required
    unk = actions[2]
    assert unk.type == "other"
    assert unk.status == "manual_required"


def test_yfinance_actions_reader():
    import pandas as pd
    from plugins.brokers.yfinance.plugin import YFinanceBrokerPlugin

    from algodesk.api import PluginManifest

    class DummyTicker:
        def __init__(self, sym):
            self.sym = sym

        @property
        def actions(self):
            idx = pd.to_datetime(["2024-08-14", "2024-10-28"])
            return pd.DataFrame(
                {
                    "Dividends": [10.0, 0.0],
                    "Stock Splits": [0.0, 2.0],
                },
                index=idx,
            )

    class DummyYF:
        def Ticker(self, sym):
            return DummyTicker(sym)

    manifest = PluginManifest.model_validate(
        {
            "name": "yfinance",
            "kind": "broker",
            "version": "0.1.0",
            "api_version": 1,
            "entry": "plugin:YFinanceBrokerPlugin",
            "capabilities": {"corporate_actions": True, "history": True},
        }
    )
    plugin = YFinanceBrokerPlugin(manifest=manifest, _yf_module=DummyYF())
    actions = plugin.actions("RELIANCE")
    assert len(actions) == 2
    assert actions[0].type == "dividend"
    assert actions[0].amount == Decimal("10.0")
    assert actions[0].ex_date == date(2024, 8, 14)
    assert actions[1].type == "split"
    assert actions[1].ratio == 2.0
    assert actions[1].ex_date == date(2024, 10, 28)


def test_corporate_actions_manager_refresh_and_ttl(tmp_path):
    from algodesk.data.actions_store import ActionsStore, CorporateActionsManager
    from algodesk.data.manifest import DataManifest

    manifest_db = tmp_path / "state" / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    store = ActionsStore(data_dir=tmp_path, manifest=manifest)

    class MockJournal:
        def __init__(self):
            self.entries = []

        def append(self, event, payload):
            self.entries.append({"event": event, "payload": payload})

    journal = MockJournal()

    # Fake broker source
    class FakeSource:
        call_count = 0

        def actions(self, symbol):
            self.call_count += 1
            return [
                ActionRecord(
                    ex_date=date(2024, 10, 28),
                    type="bonus",
                    ratio=1.0049,
                    source="upstox",
                    symbol="RELIANCE",
                    exchange="NSE",
                    fetched_at=datetime.now(UTC).isoformat(),
                )
            ]

    fake = FakeSource()
    mgr = CorporateActionsManager(
        store=store,
        sources={"upstox": fake},  # type: ignore
        journal=journal,  # type: ignore
    )

    # First refresh: fetches
    res = mgr.refresh("RELIANCE", source="upstox")
    assert fake.call_count == 1
    assert "NSE:RELIANCE" in res["refreshed"]

    # Verify journal entry was written
    actions_entries = [e for e in journal.entries if e["event"] == "data.actions"]
    assert len(actions_entries) == 1
    assert actions_entries[0]["payload"]["symbol"] == "RELIANCE"

    # Second refresh without force: skips because TTL is fresh
    res2 = mgr.refresh("RELIANCE", source="upstox")
    assert fake.call_count == 1  # Not incremented
    assert "NSE:RELIANCE" in res2["skipped"]

    # Third refresh with force: fetches again
    res3 = mgr.refresh("RELIANCE", source="upstox", force=True)
    assert fake.call_count == 2
    assert "NSE:RELIANCE" in res3["refreshed"]


def test_data_verify_actions_and_adopt(tmp_project: Path):
    from typer.testing import CliRunner

    from algodesk.cli.app import app
    from algodesk.data.actions_store import ActionsStore
    from algodesk.data.manifest import DataManifest

    data_dir = tmp_project / "data"
    manifest_db = data_dir / "state" / "data_manifest.db"
    manifest = DataManifest(manifest_db)
    store = ActionsStore(data_dir=data_dir, manifest=manifest)

    # 1. Write an action registered in manifest
    rec1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0049,
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
    )
    store.write("NSE", "RELIANCE", [rec1], source="upstox")

    # 2. Write an un-manifested (orphan) action file
    orphan_store = ActionsStore(data_dir=data_dir, manifest=None)
    rec2 = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("10.0"),
        source="yfinance",
        symbol="TCS",
        exchange="NSE",
    )
    orphan_store.write("NSE", "TCS", [rec2], source="yfinance")
    manifest.close()

    runner = CliRunner()
    # Verify detects the orphan and reports it
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "verify", "--only", "actions"])
    assert res.exit_code != 0
    assert "orphan" in res.stdout.lower()

    # Verify with --adopt succeeds
    res_adopt = runner.invoke(
        app,
        ["--root", str(tmp_project), "data", "verify", "--only", "actions", "--adopt"],
    )
    assert res_adopt.exit_code == 0

    # Manifest now has both action files
    manifest_check = DataManifest(manifest_db)
    files = manifest_check.get_files(kind="actions")
    assert len(files) == 2
    manifest_check.close()


def test_override_model_and_validation(tmp_path):
    from algodesk.api.errors import DataError
    from algodesk.data.overrides import (
        ActionOverride,
        add_override,
        load_overrides,
        override_to_action,
    )

    cfg_file = tmp_path / "config" / "corporate_actions_overrides.toml"
    ov = ActionOverride(
        symbol="RELIANCE",
        ex_date=date(2024, 10, 28),
        type="bonus",
        source="bse_filing",
        verified_on="2024-10-28",
        reason="1:1 bonus issue verified via BSE notice",
        ratio=2.0,
    )
    add_override(ov, cfg_file)
    assert cfg_file.exists()

    loaded = load_overrides(cfg_file)
    assert len(loaded) == 1
    assert loaded[0].symbol == "RELIANCE"
    assert loaded[0].ratio == 2.0
    assert loaded[0].reason == "1:1 bonus issue verified via BSE notice"

    act = override_to_action(loaded[0])
    assert act.status == "confirmed"
    assert act.ratio == 2.0
    assert act.reason == "1:1 bonus issue verified via BSE notice"

    # Missing reason or source raises DataError
    bad_file = tmp_path / "bad.toml"
    bad_file.write_text(
        '[[override]]\nsymbol = "TCS"\nex_date = "2024-01-01"\ntype = "split"\nsource = ""\n'
    )
    with pytest.raises(DataError) as exc_info:
        load_overrides(bad_file)
    assert "missing required field" in str(exc_info.value.what).lower()


def test_reconciler_confirmed_and_primary_source():
    from algodesk.data.reconciler import CorporateActionsReconciler

    r1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
        text="Upstox notice",
    )
    r2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
        text="Yahoo notice",
    )

    reconciler = CorporateActionsReconciler(primary_source="upstox")
    reconciled = reconciler.reconcile("NSE", "RELIANCE", [r1, r2], overrides=[])
    assert len(reconciled) == 1
    assert reconciled[0].status == "confirmed"
    assert reconciled[0].source == "upstox"  # primary source chosen
    assert reconciled[0].ratio == 2.0


def test_reconciler_dividend_tolerance():
    from algodesk.data.reconciler import CorporateActionsReconciler

    # Within 1.0% tolerance
    d1 = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("10.00"),
        source="upstox",
        symbol="INFY",
        exchange="NSE",
    )
    d2 = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("10.05"),  # 0.5% diff
        source="yfinance",
        symbol="INFY",
        exchange="NSE",
    )
    rec_ok = CorporateActionsReconciler(dividend_tolerance_pct=1.0)
    reconciled = rec_ok.reconcile("NSE", "INFY", [d1, d2], overrides=[])
    assert len(reconciled) == 1
    assert reconciled[0].status == "confirmed"

    # Beyond tolerance -> conflict
    d3 = ActionRecord(
        ex_date=date(2024, 8, 14),
        type="dividend",
        amount=Decimal("11.00"),  # 10% diff
        source="yfinance",
        symbol="INFY",
        exchange="NSE",
    )
    rec_bad = CorporateActionsReconciler(dividend_tolerance_pct=1.0)
    reconciled_bad = rec_bad.reconcile("NSE", "INFY", [d1, d3], overrides=[])
    assert len(reconciled_bad) == 1
    assert reconciled_bad[0].status == "conflict"


def test_reconciler_conflicts_and_unverified():
    from algodesk.data.reconciler import CorporateActionsReconciler

    # Disagree on type
    a1 = ActionRecord(
        ex_date=date(2024, 5, 10),
        type="split",
        ratio=2.0,
        source="upstox",
        symbol="SBIN",
        exchange="NSE",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 5, 10),
        type="bonus",
        ratio=2.0,
        source="yfinance",
        symbol="SBIN",
        exchange="NSE",
    )
    reconciler = CorporateActionsReconciler()
    res = reconciler.reconcile("NSE", "SBIN", [a1, a2], overrides=[])
    assert len(res) == 1
    assert res[0].status == "conflict"

    # Single source -> unverified
    res_single = reconciler.reconcile("NSE", "SBIN", [a1], overrides=[])
    assert len(res_single) == 1
    assert res_single[0].status == "unverified"

    # Unknown type -> manual_required
    a3 = ActionRecord(
        ex_date=date(2024, 6, 1),
        type="other",
        source="upstox",
        symbol="SBIN",
        exchange="NSE",
    )
    res_other = reconciler.reconcile("NSE", "SBIN", [a3], overrides=[])
    assert res_other[0].status == "manual_required"


def test_reconciler_override_wins_over_conflict():
    from algodesk.data.overrides import ActionOverride
    from algodesk.data.reconciler import CorporateActionsReconciler

    a1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0049,
        source="upstox",
        symbol="RELIANCE",
        exchange="NSE",
    )
    a2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
    )
    ov = ActionOverride(
        symbol="RELIANCE",
        exchange="NSE",
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=2.0,
        source="operator",
        verified_on="2024-10-28",
        reason="Confirmed official BSE circular",
    )

    reconciler = CorporateActionsReconciler()
    res = reconciler.reconcile("NSE", "RELIANCE", [a1, a2], overrides=[ov])
    assert len(res) == 1
    assert res[0].status == "confirmed"
    assert res[0].ratio == 2.0
    assert res[0].reason == "Confirmed official BSE circular"


def test_detect_price_anomalies_suspect_jump():
    from datetime import UTC, datetime

    from algodesk.api import InstrumentId
    from algodesk.api.market import Bar
    from algodesk.data.detection import detect_price_anomalies

    inst = InstrumentId("NSE:EQ:ABC")
    bars = [
        Bar(
            instrument=inst,
            timeframe="1d",
            start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end_ts=datetime(2024, 1, 1, 23, 59, tzinfo=UTC),
            open=100.0,
            high=105.0,
            low=95.0,
            close=100.0,
            volume=1000,
        ),
        Bar(
            instrument=inst,
            timeframe="1d",
            start_ts=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
            end_ts=datetime(2024, 1, 2, 23, 59, tzinfo=UTC),
            open=50.0,
            high=55.0,
            low=48.0,
            close=50.0,  # 50% jump down with no action!
            volume=2000,
        ),
    ]

    # Without action -> suspect_event
    items = detect_price_anomalies("ABC", bars, actions=[], detect_jump_pct=20.0)
    assert len(items) == 1
    assert items[0].kind == "suspect_event"
    assert items[0].symbol == "ABC"
    assert "jump" in items[0].details.lower()

    # With corporate action within 1 trading day -> no suspect_event
    act = ActionRecord(
        ex_date=date(2024, 1, 2),
        type="split",
        ratio=2.0,
        source="upstox",
        symbol="ABC",
        exchange="NSE",
    )
    items_with_act = detect_price_anomalies("ABC", bars, actions=[act], detect_jump_pct=20.0)
    assert not any(it.kind == "suspect_event" for it in items_with_act)


def test_detect_price_anomalies_absent_ratio_conflict():
    from datetime import UTC, datetime

    from algodesk.api import InstrumentId
    from algodesk.api.market import Bar
    from algodesk.data.detection import detect_price_anomalies

    inst = InstrumentId("NSE:EQ:XYZ")
    # Prices barely change (100 -> 98), but declared 2:1 split!
    bars = [
        Bar(
            instrument=inst,
            timeframe="1d",
            start_ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end_ts=datetime(2024, 1, 1, 23, 59, tzinfo=UTC),
            open=100.0,
            high=102.0,
            low=98.0,
            close=100.0,
            volume=1000,
        ),
        Bar(
            instrument=inst,
            timeframe="1d",
            start_ts=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
            end_ts=datetime(2024, 1, 2, 23, 59, tzinfo=UTC),
            open=98.0,
            high=99.0,
            low=97.0,
            close=98.0,
            volume=1200,
        ),
    ]
    act = ActionRecord(
        ex_date=date(2024, 1, 2),
        type="split",
        ratio=2.0,
        source="upstox",
        symbol="XYZ",
        exchange="NSE",
    )
    items = detect_price_anomalies("XYZ", bars, actions=[act], ratio_tolerance_pct=10.0)
    assert len(items) == 1
    assert items[0].kind == "conflict"
    assert "absent from prices" in items[0].details


def test_cli_actions_list_check_override_add(tmp_project):
    from typer.testing import CliRunner

    from algodesk.cli import app
    from algodesk.data.actions_store import ActionsStore

    data_dir = tmp_project / "data"
    store = ActionsStore(data_dir=data_dir)
    r1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0049,
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
    store.write("NSE", "RELIANCE", [r1, r2])

    runner = CliRunner()
    # 1. actions list shows conflict
    res_list = runner.invoke(
        app, ["--root", str(tmp_project), "data", "actions", "list", "RELIANCE"]
    )
    assert res_list.exit_code == 0
    assert "RELIANCE" in res_list.stdout
    assert "conflict" in res_list.stdout

    # 2. actions check reports conflict
    res_check = runner.invoke(
        app, ["--root", str(tmp_project), "data", "actions", "check", "RELIANCE"]
    )
    assert res_check.exit_code == 0
    assert "conflict" in res_check.stdout

    # 3. override add requires --reason
    res_bad = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "actions",
            "override",
            "add",
            "--symbol",
            "RELIANCE",
            "--ex-date",
            "2024-10-28",
            "--type",
            "bonus",
            "--ratio",
            "2.0",
        ],
    )
    assert res_bad.exit_code != 0

    # 4. override add with valid reason succeeds
    res_ov = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "data",
            "actions",
            "override",
            "add",
            "--symbol",
            "RELIANCE",
            "--ex-date",
            "2024-10-28",
            "--type",
            "bonus",
            "--ratio",
            "2.0",
            "--reason",
            "Official BSE circular confirmed",
        ],
    )
    assert res_ov.exit_code == 0
    assert "Added override for RELIANCE" in res_ov.stdout

    # 5. Now actions list and check show confirmed!
    res_list2 = runner.invoke(
        app, ["--root", str(tmp_project), "data", "actions", "list", "RELIANCE"]
    )
    assert res_list2.exit_code == 0
    assert "confirmed" in res_list2.stdout
    assert "conflict" not in res_list2.stdout
