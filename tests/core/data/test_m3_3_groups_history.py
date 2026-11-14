"""Unit tests for 'algodesk groups import-history' and historical universe resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import DataError
from algodesk.cli.app import app
from algodesk.data.groups import (
    import_history_group,
    resolve_group_universe,
    resolve_history_group,
)


def _make_dummy_master(tmp_path: Path) -> Path:
    import gzip

    master_entries = [
        {"segment": "NSE_EQ", "exchange": "NSE", "trading_symbol": "RELIANCE"},
        {"segment": "NSE_EQ", "exchange": "NSE", "trading_symbol": "TCS"},
        {"segment": "NSE_EQ", "exchange": "NSE", "trading_symbol": "INFY"},
        {"segment": "NSE_EQ", "exchange": "NSE", "trading_symbol": "HDFCBANK"},
    ]
    cache_dir = tmp_path / ".local" / "cache" / "instruments"
    cache_dir.mkdir(parents=True, exist_ok=True)
    gz_p = cache_dir / "upstox_master_2026-11-13.json.gz"
    gz_p.write_bytes(gzip.compress(json.dumps(master_entries).encode("utf-8")))
    return gz_p


def test_history_import_iso_date_enforcement(tmp_project: Any) -> None:
    csv_file = tmp_project / "bad_date.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '1998/01/08,Nifty50,,,"RELIANCE,TCS"\n',
        encoding="utf-8",
    )

    with pytest.raises(DataError) as exc:
        import_history_group(
            csv_path=csv_file,
            name="test_hist",
            index_type="Nifty50",
            universe_dir=tmp_project / "data" / "universe",
        )

    assert "Invalid non-ISO date '1998/01/08' at line 2" in exc.value.what


def test_history_import_refuses_missing_index_type(tmp_project: Any) -> None:
    csv_file = tmp_project / "index_type_test.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '2020-01-01,Nifty50,,,"RELIANCE,TCS"\n',
        encoding="utf-8",
    )

    with pytest.raises(DataError) as exc:
        import_history_group(
            csv_path=csv_file,
            name="test_hist",
            index_type="NonExistentIndex",
            universe_dir=tmp_project / "data" / "universe",
        )

    assert "No rows matching index_type 'NonExistentIndex'" in exc.value.what
    assert "Nifty50" in exc.value.fix


def test_history_import_detects_inconsistencies(tmp_project: Any) -> None:
    # Row 2 adds INFY but does not list it in inclusions
    csv_file = tmp_project / "inconsistent.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '2020-01-01,Nifty50,,,"RELIANCE,TCS"\n'
        '2020-06-01,Nifty50,,,"RELIANCE,TCS,INFY"\n',
        encoding="utf-8",
    )

    # In permissive mode (default), mismatches are written to sidecar JSON and never fatal
    u_dir = tmp_project / "data" / "universe"
    res = import_history_group(
        csv_path=csv_file,
        name="test_hist",
        index_type="Nifty50",
        universe_dir=u_dir,
    )
    assert len(res.inconsistencies) == 1
    sidecar_p = u_dir / "test_hist.inconsistencies.json"
    assert sidecar_p.is_file()

    # In strict mode, mismatches are fatal
    with pytest.raises(DataError) as exc:
        import_history_group(
            csv_path=csv_file,
            name="test_hist",
            index_type="Nifty50",
            universe_dir=u_dir,
            strict=True,
        )

    assert "list inconsistencies" in exc.value.what
    assert "--strict" in exc.value.fix


def test_history_import_accept_inconsistent_flag(tmp_project: Any) -> None:
    csv_file = tmp_project / "inconsistent_accept.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '2020-01-01,Nifty50,,,"RELIANCE,TCS"\n'
        '2020-06-01,Nifty50,,,"RELIANCE,TCS,INFY"\n',
        encoding="utf-8",
    )

    res = import_history_group(
        csv_path=csv_file,
        name="test_hist",
        index_type="Nifty50",
        universe_dir=tmp_project / "data" / "universe",
        accept_inconsistent=True,
    )

    assert res.total_rows == 2
    assert len(res.inconsistencies) == 1
    assert res.inconsistencies[0]["date"] == "2020-06-01"
    assert res.parquet_file.is_file()


def test_history_import_builds_membership_intervals(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    # Timeline:
    # 2020-01-01: RELIANCE, TCS
    # 2021-01-01: TCS excluded, INFY included -> RELIANCE, INFY
    # 2022-01-01: HDFCBANK added -> RELIANCE, INFY, HDFCBANK
    csv_file = tmp_project / "timeline.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '2020-01-01,NiftyTest,,,"RELIANCE,TCS"\n'
        '2021-01-01,NiftyTest,INFY,TCS,"RELIANCE,INFY"\n'
        '2022-01-01,NiftyTest,HDFCBANK,,"RELIANCE,INFY,HDFCBANK"\n',
        encoding="utf-8",
    )

    u_dir = tmp_project / "data" / "universe"
    res = import_history_group(
        csv_path=csv_file,
        name="nifty_timeline",
        index_type="NiftyTest",
        universe_dir=u_dir,
        data_dir=tmp_project / "data",
    )

    assert res.intervals_count == 4  # RELIANCE, TCS, INFY, HDFCBANK

    # Verify resolution as of 2020-06-01 (RELIANCE, TCS)
    m_2020 = resolve_history_group("nifty_timeline", "2020-06-01", universe_dir=u_dir)
    assert m_2020["members"] == ["RELIANCE", "TCS"]
    assert m_2020["total_members"] == 2
    assert m_2020["coverage_pct"] == 100.0

    # Verify resolution as of 2021-06-01 (RELIANCE, INFY)
    m_2021 = resolve_history_group("nifty_timeline", "2021-06-01", universe_dir=u_dir)
    assert m_2021["members"] == ["INFY", "RELIANCE"]

    # Verify resolution as of 2022-06-01 (HDFCBANK, INFY, RELIANCE)
    m_2022 = resolve_history_group("nifty_timeline", "2022-06-01", universe_dir=u_dir)
    assert m_2022["members"] == ["HDFCBANK", "INFY", "RELIANCE"]


def test_history_import_compares_with_current_group(tmp_project: Any) -> None:
    # Current group has RELIANCE, TCS, INFY
    groups_dir = tmp_project / "config" / "groups"
    groups_dir.mkdir(parents=True, exist_ok=True)
    (groups_dir / "nifty50.toml").write_text(
        'name = "nifty50"\nmembers = ["NSE:EQ:RELIANCE", "NSE:EQ:TCS", "NSE:EQ:INFY"]\n',
        encoding="utf-8",
    )

    # History last row has RELIANCE, TCS, SBIN
    csv_file = tmp_project / "diff_history.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '2026-09-30,Nifty50,,,"RELIANCE,TCS,SBIN"\n',
        encoding="utf-8",
    )

    res = import_history_group(
        csv_path=csv_file,
        name="nifty50_history",
        index_type="Nifty50",
        universe_dir=tmp_project / "data" / "universe",
        groups_dir=groups_dir,
    )

    assert res.comparison is not None
    assert res.comparison["current_group"] == "nifty50"
    assert res.comparison["in_history_not_current"] == ["SBIN"]
    assert res.comparison["in_current_not_history"] == ["INFY"]


def test_groups_show_as_of_cli(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    csv_file = tmp_project / "cli_hist.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '2020-01-01,Nifty50,,,"RELIANCE,TCS"\n'
        '2024-01-01,Nifty50,"INFY,DUMMYUNKNOWN",TCS,"RELIANCE,INFY,DUMMYUNKNOWN"\n',
        encoding="utf-8",
    )

    u_dir = tmp_project / "data" / "universe"
    import_history_group(
        csv_path=csv_file,
        name="cli_hist",
        index_type="Nifty50",
        universe_dir=u_dir,
        data_dir=tmp_project / "data",
    )

    runner = CliRunner()
    # Query as of 2020-05-01
    res = runner.invoke(app, ["groups", "show", "cli_hist", "--as-of", "2020-05-01"])
    assert res.exit_code == 0
    assert "Group: cli_hist (as-of 2020-05-01)" in res.output
    assert "RELIANCE" in res.output
    assert "TCS" in res.output

    # Query as of 2024-03-28
    res2 = runner.invoke(app, ["groups", "show", "cli_hist", "--as-of", "2024-03-28"])
    assert res2.exit_code == 0
    assert "Members: 3" in res2.output
    assert "Coverage: 66.7%" in res2.output  # 2 mapped out of 3

    # Query resolved
    res_res = runner.invoke(
        app, ["groups", "show", "cli_hist", "--as-of", "2024-03-28", "--resolved"]
    )
    assert res_res.exit_code == 0
    assert "NSE:EQ:RELIANCE" in res_res.output
    assert "Resolved Hash:" in res_res.output

    # Query JSON
    res_json = runner.invoke(app, ["groups", "show", "cli_hist", "--as-of", "2024-03-28", "--json"])
    assert res_json.exit_code == 0
    data = json.loads(res_json.output)
    assert data["total_members"] == 3
    assert data["mapped_count"] == 2
    assert data["unmapped_count"] == 1


def test_resolve_group_universe_with_as_of(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    csv_file = tmp_project / "univ_hist.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '2020-01-01,Nifty50,,,"RELIANCE,TCS"\n',
        encoding="utf-8",
    )

    u_dir = tmp_project / "data" / "universe"
    import_history_group(
        csv_path=csv_file,
        name="univ_hist",
        index_type="Nifty50",
        universe_dir=u_dir,
        data_dir=tmp_project / "data",
    )

    canonical, content_hash = resolve_group_universe(
        "univ_hist",
        as_of="2020-05-01",
        universe_dir=u_dir,
    )
    assert canonical == ["NSE:EQ:RELIANCE", "NSE:EQ:TCS"]
    assert len(content_hash) == 16


def test_history_import_missing_csv(tmp_project: Any) -> None:
    non_existent = tmp_project / "non_existent_hist.csv"
    with pytest.raises(DataError) as exc:
        import_history_group(
            csv_path=non_existent,
            name="hist_missing",
            index_type="Nifty50",
            universe_dir=tmp_project / "data" / "universe",
        )
    assert "not found" in exc.value.what


def test_history_import_empty_rows(tmp_project: Any) -> None:
    empty_csv = tmp_project / "empty_hist.csv"
    empty_csv.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n", encoding="utf-8"
    )
    with pytest.raises(DataError) as exc:
        import_history_group(
            csv_path=empty_csv,
            name="hist_empty",
            index_type="Nifty50",
            universe_dir=tmp_project / "data" / "universe",
        )
    assert "No rows matching index_type" in exc.value.what


def test_history_as_of_before_start_returns_empty(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    csv_file = tmp_project / "early_hist.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '2020-01-01,Nifty50,,,"RELIANCE,TCS"\n',
        encoding="utf-8",
    )
    u_dir = tmp_project / "data" / "universe"
    import_history_group(
        csv_path=csv_file,
        name="early_hist",
        index_type="Nifty50",
        universe_dir=u_dir,
        data_dir=tmp_project / "data",
    )
    # Query before 2020-01-01
    res = resolve_history_group("early_hist", "2019-12-31", universe_dir=u_dir)
    assert res["total_members"] == 0
    assert res["members"] == []


def test_history_show_cli_missing_group_fails(tmp_project: Any) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["groups", "show", "non_existent_group_xyz", "--as-of", "2024-01-01"])
    assert res.exit_code != 0
    assert "not found" in res.output


def test_history_import_fatal_repeated_date(tmp_project: Any) -> None:
    csv_file = tmp_project / "repeated_date.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '2020-01-01,Nifty50,,,"RELIANCE,TCS"\n'
        '2020-01-01,Nifty50,,,"RELIANCE,TCS"\n',
        encoding="utf-8",
    )
    with pytest.raises(DataError) as exc:
        import_history_group(
            csv_path=csv_file,
            name="test_repeated",
            index_type="Nifty50",
            universe_dir=tmp_project / "data" / "universe",
        )
    assert "Repeated effective date '2020-01-01'" in exc.value.what


def test_history_import_fatal_decreasing_date(tmp_project: Any) -> None:
    csv_file = tmp_project / "decreasing_date.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '2020-06-01,Nifty50,,,"RELIANCE,TCS"\n'
        '2020-01-01,Nifty50,,,"RELIANCE,TCS"\n',
        encoding="utf-8",
    )
    with pytest.raises(DataError) as exc:
        import_history_group(
            csv_path=csv_file,
            name="test_decreasing",
            index_type="Nifty50",
            universe_dir=tmp_project / "data" / "universe",
        )
    assert "Decreasing effective date '2020-01-01'" in exc.value.what


def test_history_import_fatal_empty_snapshot(tmp_project: Any) -> None:
    csv_file = tmp_project / "empty_snapshot.csv"
    csv_file.write_text(
        'effective_date,index_type,inclusions,exclusions,symbols\n2020-01-01,Nifty50,,,""\n',
        encoding="utf-8",
    )
    with pytest.raises(DataError) as exc:
        import_history_group(
            csv_path=csv_file,
            name="test_empty_snap",
            index_type="Nifty50",
            universe_dir=tmp_project / "data" / "universe",
        )
    assert "Empty snapshot on 2020-01-01" in exc.value.what


def test_history_import_fatal_size_jump(tmp_project: Any) -> None:
    csv_file = tmp_project / "size_jump.csv"
    # Day 1 has 5 symbols, Day 2 has 106 symbols (jump = 101 > 100)
    day1_syms = ",".join(f"SYM{i}" for i in range(5))
    day2_syms = ",".join(f"SYM{i}" for i in range(106))
    csv_file.write_text(
        f"effective_date,index_type,inclusions,exclusions,symbols\n"
        f'2020-01-01,Nifty50,,,"{day1_syms}"\n'
        f'2020-06-01,Nifty50,,,"{day2_syms}"\n',
        encoding="utf-8",
    )
    with pytest.raises(DataError) as exc:
        import_history_group(
            csv_path=csv_file,
            name="test_jump",
            index_type="Nifty50",
            universe_dir=tmp_project / "data" / "universe",
        )
    assert "exceeds maximum allowed jump of 100" in exc.value.what


def test_history_import_strict_flag_cli(tmp_project: Any) -> None:
    csv_file = tmp_project / "inconsistent_cli.csv"
    csv_file.write_text(
        "effective_date,index_type,inclusions,exclusions,symbols\n"
        '2020-01-01,Nifty50,,,"RELIANCE,TCS"\n'
        '2020-06-01,Nifty50,,,"RELIANCE,TCS,INFY"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    # Without strict: succeeds
    res_ok = runner.invoke(
        app,
        [
            "groups",
            "import-history",
            str(csv_file),
            "--name",
            "cli_inconsistent",
            "--index",
            "Nifty50",
        ],
    )
    assert res_ok.exit_code == 0
    assert (
        "List inconsistencies: 1 (written to cli_inconsistent.inconsistencies.json)"
        in res_ok.output
    )

    # With strict: fails
    res_fail = runner.invoke(
        app,
        [
            "groups",
            "import-history",
            str(csv_file),
            "--name",
            "cli_inconsistent_strict",
            "--index",
            "Nifty50",
            "--strict",
        ],
    )
    assert res_fail.exit_code != 0
    assert "list inconsistencies" in res_fail.output
