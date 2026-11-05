"""Additional tests for M2-3b CP4: CLI, resolution, diffing, feasibility, and replay."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from plugins.brokers.yfinance.plugin import YFinanceBrokerPlugin
from typer.testing import CliRunner

from algodesk.api.alias import parse_legacy_symbol, register_alias, resolve_alias
from algodesk.api.errors import DataError, NotSupported
from algodesk.api.market import (
    Instrument,
    InstrumentId,
    format_id,
    parse_id,
)
from algodesk.cli.app import app
from algodesk.data.contracts import ContractResolver
from algodesk.data.groups import resolve_group_universe
from algodesk.data.instruments_store import InstrumentStore
from algodesk.data.layout import (
    parse_bar_file_path,
    parse_tick_file_path,
)
from algodesk.data.manifest import DataManifest
from algodesk.data.migration import (
    build_migration_plan,
)
from algodesk.ops.checks import (
    _check_instrument_master_diff,
    _check_legacy_instrument_ids,
)
from algodesk.ops.runtime import open_runtime


def test_cli_data_migrate_ids_dry_run_lists_plan() -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["data", "migrate-ids", "--dry-run"])
    assert res.exit_code == 0
    assert "Migration Plan" in res.output or "No legacy files" in res.output


def test_cli_data_migrate_ids_json() -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["data", "migrate-ids", "--dry-run", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert "total_files" in data
    assert "items" in data


def test_cli_instruments_diff_help() -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["instruments", "diff", "--help"])
    assert res.exit_code == 0
    assert "Compare daily instrument master snapshots" in res.output


def test_cli_instruments_feasibility_help() -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["instruments", "feasibility", "--help"])
    assert res.exit_code == 0
    assert "feasibility" in res.output.lower()


def test_cli_groups_show_resolved() -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["groups", "show", "demo5", "--resolved"])
    assert res.exit_code == 0
    assert "Resolved Hash" in res.output or "Group: demo5" in res.output


def test_cli_groups_show_resolved_json() -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["groups", "show", "demo5", "--resolved", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert "hash" in data
    assert "members" in data
    assert isinstance(data["members"], list)


def test_format_and_parse_id_errors() -> None:
    from algodesk.api.market import IdParts

    with pytest.raises(ValueError, match="must be non-empty"):
        parse_id("")
    with pytest.raises(ValueError, match="Expected 3, 4, or 6"):
        parse_id("NSE:EQ")
    with pytest.raises(ValueError, match="Segment must be non-empty"):
        format_id(IdParts(exchange="NSE", segment="", symbol="TCS"))
    with pytest.raises(ValueError, match="Strike must be a positive Decimal"):
        format_id(
            IdParts(
                exchange="NSE",
                segment="OPT",
                symbol="NIFTY",
                expiry=date(2026, 12, 31),
                strike=None,
                option_type="CE",
            )
        )


def test_parse_legacy_symbol_variations() -> None:
    fut = parse_legacy_symbol("BANKNIFTY FUT 29 DEC 26")
    assert fut is not None
    assert fut[0] == "FUT"
    assert fut[1] == "BANKNIFTY"
    assert fut[2] == date(2026, 12, 29)

    opt = parse_legacy_symbol("BANKNIFTY 50000 CE 29 DEC 26")
    assert opt is not None
    assert opt[0] == "OPT"
    assert opt[1] == "BANKNIFTY"
    assert opt[2] == date(2026, 12, 29)
    assert opt[3] == Decimal("50000")
    assert opt[4] == "CE"

    assert parse_legacy_symbol("INVALID SYMBOL STRING") is None


def test_parse_tick_and_bar_file_paths() -> None:
    p_eq = Path("data/ticks/exchange=NSE/segment=EQ/symbol=RELIANCE/date=2026-11-04/day.parquet")
    inst_eq = parse_tick_file_path(p_eq)
    assert inst_eq is not None
    assert inst_eq["instrument"] == "NSE:EQ:RELIANCE"

    p_fut = Path(
        "data/ticks/exchange=MCX/segment=FUT/symbol=GOLD/expiry=2026-12-04/date=2026-11-04/day.parquet"
    )
    inst_fut = parse_tick_file_path(p_fut)
    assert inst_fut is not None
    assert inst_fut["instrument"] == "MCX:FUT:GOLD:2026-12-04"

    p_opt = Path(
        "data/ticks/exchange=NSE/segment=OPT/symbol=NIFTY/expiry=2026-12-31/strike=24000/type=CE/date=2026-11-04/day.parquet"
    )
    inst_opt = parse_tick_file_path(p_opt)
    assert inst_opt is not None
    assert inst_opt["instrument"] == "NSE:OPT:NIFTY:2026-12-31:24000:CE"

    b_p = Path(
        "data/bars/source=upstox/timeframe=1m/exchange=NSE/segment=FUT/symbol=NIFTY/expiry=2026-12-31/adj=raw/year=2026/2026-12.parquet"
    )
    b_inst = parse_bar_file_path(b_p)
    assert b_inst is not None
    assert b_inst["instrument"] == "NSE:FUT:NIFTY:2026-12-31"


def test_yfinance_plugin_non_equity_raises_not_supported() -> None:
    yf = YFinanceBrokerPlugin(MagicMock())
    with pytest.raises(NotSupported, match="maps equity only"):
        yf.map_symbol(
            Instrument(
                exchange="NSE",
                segment="FUT",
                symbol="NIFTY",
                expiry=date(2026, 12, 31),
                lot_size=50,
                tick_size=Decimal("0.05"),
            )
        )


def test_contract_resolver_mid_series_lot_size_change() -> None:
    # Contract series where later contract has adjusted lot size
    c1 = Instrument(
        exchange="NSE",
        segment="FUT",
        symbol="ABC",
        expiry=date(2026, 11, 26),
        lot_size=100,
        tick_size=Decimal("0.05"),
    )
    c2 = Instrument(
        exchange="NSE",
        segment="FUT",
        symbol="ABC",
        expiry=date(2026, 12, 31),
        lot_size=75,  # adjusted lot size
        tick_size=Decimal("0.05"),
    )
    resolver = ContractResolver([c1, c2])
    near = resolver.resolve_contract("ABC", "future", "near", as_of=date(2026, 11, 1))
    assert near.lot_size == 100
    nxt = resolver.resolve_contract("ABC", "future", "next", as_of=date(2026, 11, 1))
    assert nxt.lot_size == 75


def test_contract_resolver_expired_filtering() -> None:
    c1 = Instrument(
        exchange="NSE",
        segment="FUT",
        symbol="XYZ",
        expiry=date(2026, 10, 29),  # already expired
        lot_size=50,
        tick_size=Decimal("0.05"),
    )
    c2 = Instrument(
        exchange="NSE",
        segment="FUT",
        symbol="XYZ",
        expiry=date(2026, 11, 26),
        lot_size=50,
        tick_size=Decimal("0.05"),
    )
    resolver = ContractResolver([c1, c2])
    res = resolver.resolve_contract("XYZ", "future", "near", as_of=date(2026, 11, 1))
    assert res.expiry == date(2026, 11, 26)


def test_contract_resolver_strike_matching_nearest() -> None:
    opt1 = Instrument(
        exchange="NSE",
        segment="OPT",
        underlying="NIFTY",
        symbol="NIFTY",
        expiry=date(2026, 11, 26),
        strike=Decimal("24000"),
        option_type="CE",
        lot_size=50,
        tick_size=Decimal("0.05"),
    )
    opt2 = Instrument(
        exchange="NSE",
        segment="OPT",
        underlying="NIFTY",
        symbol="NIFTY",
        expiry=date(2026, 11, 26),
        strike=Decimal("24100"),
        option_type="CE",
        lot_size=50,
        tick_size=Decimal("0.05"),
    )
    resolver = ContractResolver([opt1, opt2])
    res = resolver.resolve_contract(
        "NIFTY",
        "option",
        {"strike": Decimal("24000"), "type": "CE", "expiry": date(2026, 11, 26)},
        as_of=date(2026, 11, 1),
    )
    assert res.strike == Decimal("24000")


def test_doctor_check_legacy_ids_logic(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db_file = data_dir / "state" / "data_manifest.db"
    _manifest = DataManifest(db_file)

    with open_runtime(tmp_path, ensure=False) as rt:
        res = _check_legacy_instrument_ids(rt)
        assert res.ok is True

        # Introduce a legacy partition
        leg_dir = (
            data_dir
            / "ticks"
            / "exchange=MCX"
            / "segment=FNO"
            / "symbol=GOLD FUT 04 DEC 26"
            / "date=2026-11-04"
        )
        leg_dir.mkdir(parents=True)
        leg_file = leg_dir / "day.parquet"
        leg_file.write_bytes(b"dummy")

        res_after = _check_legacy_instrument_ids(rt)
        assert res_after.ok is False
        assert "remain across legacy identifiers" in res_after.message


def test_doctor_check_diff_warnings_logic(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    with open_runtime(tmp_path, ensure=False) as rt:
        res = _check_instrument_master_diff(rt)
        assert res.ok is True


def test_frozen_universes_order_independence(tmp_path: Path) -> None:
    g_dir = tmp_path / "groups"
    g_dir.mkdir(parents=True)
    (g_dir / "test.toml").write_text('members = ["NSE:EQ:TCS", "NSE:EQ:INFY", "NSE:EQ:RELIANCE"]\n')
    (g_dir / "test_rev.toml").write_text(
        'members = ["NSE:EQ:RELIANCE", "NSE:EQ:TCS", "NSE:EQ:INFY"]\n'
    )

    u1, h1 = resolve_group_universe("test", groups_dir=g_dir)
    u2, h2 = resolve_group_universe("test_rev", groups_dir=g_dir)
    assert u1 == u2
    assert h1 == h2


def test_instrument_store_checksum_verification(tmp_path: Path) -> None:
    store = InstrumentStore(tmp_path)
    # Write snapshot with corrupted checksum
    day_dir = tmp_path / "instruments" / "source=test" / "date=2026-11-01"
    day_dir.mkdir(parents=True)
    (day_dir / "master.json.gz").write_bytes(b"content")
    (day_dir / "checksum.sha256").write_text("badchecksum1234567890\n")

    with pytest.raises(DataError, match="Checksum mismatch"):
        store.load_master("test", date(2026, 11, 1))


def test_alias_registry_explicit_mapping() -> None:
    register_alias("MY_CUSTOM_ALIAS", "NSE:EQ:RELIANCE")
    assert resolve_alias("MY_CUSTOM_ALIAS") == InstrumentId("NSE:EQ:RELIANCE")


def test_migration_plan_empty_when_no_legacy(tmp_path: Path) -> None:
    db_file = tmp_path / "state" / "data_manifest.db"
    manifest = DataManifest(db_file)
    plan = build_migration_plan(tmp_path, manifest)
    assert plan.total_files == 0
    assert len(plan.items) == 0


def test_contract_resolver_empty_contracts_fails() -> None:
    resolver = ContractResolver([])
    with pytest.raises(DataError, match="No active FUT contracts found"):
        resolver.resolve_contract("NIFTY", "future", "near", as_of=date(2026, 11, 1))


def test_contract_resolver_holiday_adjustment_to_prior_day() -> None:
    # Expiry is Sunday Nov 8. Previous trading day is Friday Nov 6.
    fut = Instrument(
        exchange="NSE",
        segment="FUT",
        symbol="NIFTY",
        expiry=date(2026, 11, 8),
        lot_size=50,
        tick_size=Decimal("0.05"),
    )
    cal = MagicMock()
    # Sunday (8) and Saturday (7) are holidays. Friday (6) is trading day.
    cal.is_trading_day.side_effect = lambda d: d not in (date(2026, 11, 7), date(2026, 11, 8))
    resolver = ContractResolver([fut], calendar=cal)
    res = resolver.resolve_contract("NIFTY", "future", "near", as_of=date(2026, 11, 1))
    assert res.id == fut.id


def test_instrument_store_diff_all_change_types(tmp_path: Path) -> None:
    store = InstrumentStore(tmp_path)
    d1 = [
        {
            "exchange": "NSE",
            "segment": "EQ",
            "symbol": "INFY",
            "lot_size": 1,
            "tick_size": 0.05,
            "isin": "INE009A01021",
        },
        {
            "exchange": "NSE",
            "segment": "EQ",
            "symbol": "TCS",
            "lot_size": 1,
            "tick_size": 0.05,
            "isin": "INE467B01029",
        },
    ]
    d2 = [
        # TCS lot size changed, tick size changed
        {
            "exchange": "NSE",
            "segment": "EQ",
            "symbol": "TCS",
            "lot_size": 2,
            "tick_size": 0.10,
            "isin": "INE467B01029",
        },
        # RELIANCE added
        {
            "exchange": "NSE",
            "segment": "EQ",
            "symbol": "RELIANCE",
            "lot_size": 1,
            "tick_size": 0.05,
            "isin": "INE002A01018",
        },
        # INFY removed
    ]
    store.save_master("test", date(2026, 11, 1), json.dumps(d1).encode("utf-8"))
    store.save_master("test", date(2026, 11, 2), json.dumps(d2).encode("utf-8"))

    diff = store.diff("test", date(2026, 11, 1), date(2026, 11, 2))
    assert len(diff.added) == 1
    assert diff.added[0].symbol == "RELIANCE"
    assert len(diff.removed) == 1
    assert diff.removed[0].symbol == "INFY"
    assert len(diff.changed) == 1
    assert diff.changed[0].id == InstrumentId("NSE:EQ:TCS")
    assert "lot_size" in diff.changed[0].changes
    assert "tick_size" in diff.changed[0].changes


def test_alias_map_with_options_and_decimals() -> None:
    res = resolve_alias("NSE:FNO:BANKNIFTY 50000.5 CE 29 DEC 26")
    assert res == InstrumentId("NSE:OPT:BANKNIFTY:2026-12-29:50000.5:CE")


def test_instrument_kind_property_detection() -> None:
    eq = Instrument(exchange="NSE", segment="EQ", symbol="INFY")
    assert eq.kind == "equity"
    idx = Instrument(exchange="NSE", segment="INDEX", symbol="NIFTY 50")
    assert idx.kind == "index"
    fut = Instrument(exchange="NSE", segment="FUT", symbol="NIFTY", expiry=date(2026, 12, 31))
    assert fut.kind == "future"
    opt = Instrument(
        exchange="NSE",
        segment="OPT",
        symbol="NIFTY",
        expiry=date(2026, 12, 31),
        strike=Decimal("24000"),
        option_type="CE",
    )
    assert opt.kind == "option"


def test_replay_net_pnl_baseline_exact() -> None:
    """Ensure baseline replay net P&L remains identical."""
    import shutil

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "replay",
            "--session",
            "demo_paper_fast_paper_20261102_055142_356d14",
            "--run",
            "demo_paper_fast",
        ],
    )
    assert res.exit_code == 0
    res_path = None
    for line in res.output.splitlines():
        if "results folder:" in line:
            res_path = Path(line.split("results folder:")[-1].strip())
            break
    assert res_path is not None and res_path.exists()
    try:
        metrics = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
        assert Decimal(str(metrics["net_pnl"])) == Decimal("-30.02")
        assert Decimal(str(metrics["gross_pnl"])) == Decimal("-7.25")
        assert metrics["trades"] == 29
    finally:
        shutil.rmtree(res_path, ignore_errors=True)
