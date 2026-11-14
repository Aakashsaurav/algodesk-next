"""Unit tests for 'algodesk groups import' and current index group loading."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import DataError
from algodesk.cli.app import app
from algodesk.data.groups import import_group, list_group_details, load_groups


def _make_dummy_master(tmp_path: Path) -> Path:
    import gzip

    master_entries = [
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "trading_symbol": "RELIANCE",
            "isin": "INE002A01018",
        },
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "trading_symbol": "TCS",
            "isin": "INE467B01029",
        },
        {
            "segment": "NSE_EQ",
            "exchange": "NSE",
            "trading_symbol": "INFY",
            "isin": "INE009A01021",
        },
    ]
    cache_dir = tmp_path / ".local" / "cache" / "instruments"
    cache_dir.mkdir(parents=True, exist_ok=True)
    gz_p = cache_dir / "upstox_master_2026-11-13.json.gz"
    gz_p.write_bytes(gzip.compress(json.dumps(master_entries).encode("utf-8")))
    return gz_p


def test_group_import_detects_nse_header(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    csv_file = tmp_project / "test_current.csv"
    csv_file.write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Oil & Gas,RELIANCE,EQ,INE002A01018\n"
        "Tata Consultancy Services,IT,TCS,EQ,INE467B01029\n",
        encoding="utf-8",
    )

    res = import_group(
        csv_path=csv_file,
        group_name="test_index",
        groups_dir=tmp_project / "config" / "groups",
        data_dir=tmp_project / "data",
    )

    assert res.group_name == "test_index"
    assert res.total_rows == 2
    assert res.mapped_count == 2
    assert res.unmapped_count == 0
    assert "NSE:EQ:RELIANCE" in res.mapped_members
    assert "NSE:EQ:TCS" in res.mapped_members
    assert res.group_file.is_file()
    assert res.sidecar_file.is_file()


def test_group_import_refuses_invalid_header(tmp_project: Any) -> None:
    csv_file = tmp_project / "bad_header.csv"
    csv_file.write_text("Ticker,Name,Price\nAAA,Apple,150\n", encoding="utf-8")

    with pytest.raises(DataError) as exc:
        import_group(
            csv_path=csv_file,
            group_name="bad_group",
            groups_dir=tmp_project / "config" / "groups",
        )

    assert "Unrecognized CSV format" in exc.value.what
    assert "Symbol" in exc.value.what


def test_group_import_maps_by_isin_then_symbol(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    # INFY matches by Symbol only (ISIN is blank)
    csv_file = tmp_project / "mix.csv"
    csv_file.write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Oil & Gas,RELIANCE,EQ,INE002A01018\n"
        "Infosys Ltd.,IT,INFY,EQ,\n",
        encoding="utf-8",
    )

    res = import_group(
        csv_path=csv_file,
        group_name="mix_group",
        groups_dir=tmp_project / "config" / "groups",
        data_dir=tmp_project / "data",
    )

    assert res.mapped_count == 2
    assert "NSE:EQ:RELIANCE" in res.mapped_members
    assert "NSE:EQ:INFY" in res.mapped_members


def test_group_import_unmapped_threshold_refusal(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    # 2 out of 3 unmapped = 66.7% > 5%
    csv_file = tmp_project / "unmapped.csv"
    csv_file.write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Oil & Gas,RELIANCE,EQ,INE002A01018\n"
        "Unknown Corp 1,None,UNKNOWN1,EQ,DUMMYISIN1\n"
        "Unknown Corp 2,None,UNKNOWN2,EQ,DUMMYISIN2\n",
        encoding="utf-8",
    )

    with pytest.raises(DataError) as exc:
        import_group(
            csv_path=csv_file,
            group_name="fail_group",
            groups_dir=tmp_project / "config" / "groups",
            data_dir=tmp_project / "data",
        )

    assert "exceeds maximum allowed" in exc.value.what
    assert "--accept-unmapped" in exc.value.fix


def test_group_import_accept_unmapped_flag(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    csv_file = tmp_project / "unmapped_allowed.csv"
    csv_file.write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Oil & Gas,RELIANCE,EQ,INE002A01018\n"
        "Unknown Corp 1,None,UNKNOWN1,EQ,DUMMYISIN1\n",
        encoding="utf-8",
    )

    res = import_group(
        csv_path=csv_file,
        group_name="allowed_group",
        groups_dir=tmp_project / "config" / "groups",
        accept_unmapped=True,
        data_dir=tmp_project / "data",
    )

    assert res.mapped_count == 1
    assert res.unmapped_count == 1
    assert len(res.unmapped_members) == 1
    assert res.unmapped_members[0]["symbol"] == "UNKNOWN1"
    sidecar_data = json.loads(res.sidecar_file.read_text(encoding="utf-8"))
    assert sidecar_data["unmapped_count"] == 1


def test_group_import_journals_sha256(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    csv_file = tmp_project / "journal_test.csv"
    csv_file.write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Oil & Gas,RELIANCE,EQ,INE002A01018\n",
        encoding="utf-8",
    )

    class MockJournal:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def append(self, kind: str, payload: dict[str, Any], durable: bool = True) -> None:
            self.events.append((kind, payload))

    j = MockJournal()
    res = import_group(
        csv_path=csv_file,
        group_name="j_group",
        groups_dir=tmp_project / "config" / "groups",
        journal=j,
        data_dir=tmp_project / "data",
    )

    assert len(j.events) == 1
    kind, payload = j.events[0]
    assert kind == "groups.import"
    assert payload["group"] == "j_group"
    assert payload["sha256"] == res.source_sha256


def test_group_import_cli(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    csv_file = tmp_project / "cli_test.csv"
    csv_file.write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Oil & Gas,RELIANCE,EQ,INE002A01018\n"
        "Tata Consultancy Services,IT,TCS,EQ,INE467B01029\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    res = runner.invoke(app, ["groups", "import", str(csv_file), "--name", "cli_group"])
    assert res.exit_code == 0
    assert "Imported group 'cli_group'" in res.output
    assert "Mapped: 2" in res.output

    # Check CLI show
    res_show = runner.invoke(app, ["groups", "show", "cli_group"])
    assert res_show.exit_code == 0
    assert "NSE:EQ:RELIANCE" in res_show.output


def test_group_import_cli_json(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    csv_file = tmp_project / "cli_json.csv"
    csv_file.write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Oil & Gas,RELIANCE,EQ,INE002A01018\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    res = runner.invoke(app, ["groups", "import", str(csv_file), "--name", "json_group", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["group"] == "json_group"
    assert data["mapped_count"] == 1


def test_load_groups_ignores_unmapped_files(tmp_project: Any) -> None:
    groups_dir = tmp_project / "config" / "groups"
    groups_dir.mkdir(parents=True, exist_ok=True)
    (groups_dir / "valid.toml").write_text('name = "valid"\nmembers = ["NSE:EQ:A"]\n')
    (groups_dir / "valid.unmapped.json").write_text('{"unmapped": []}')
    (groups_dir / "valid.unmapped.toml").write_text("junk = true")

    loaded = load_groups(groups_dir)
    assert "valid" in loaded
    assert "valid.unmapped" not in loaded

    details = list_group_details(groups_dir)
    assert "valid" in details
    assert "valid.unmapped" not in details


def test_group_import_missing_file_raises_data_error(tmp_project: Any) -> None:
    non_existent = tmp_project / "non_existent.csv"
    with pytest.raises(DataError) as exc:
        import_group(
            csv_path=non_existent,
            group_name="none_group",
            groups_dir=tmp_project / "config" / "groups",
        )
    assert "not found" in exc.value.what


def test_group_import_empty_csv_raises_data_error(tmp_project: Any) -> None:
    empty_csv = tmp_project / "empty.csv"
    empty_csv.write_text("Company Name,Industry,Symbol,Series,ISIN Code\n", encoding="utf-8")
    with pytest.raises(DataError) as exc:
        import_group(
            csv_path=empty_csv,
            group_name="empty_group",
            groups_dir=tmp_project / "config" / "groups",
        )
    assert "No data rows found" in exc.value.what


def test_group_import_case_insensitive_header(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    csv_file = tmp_project / "lower_case.csv"
    csv_file.write_text(
        "company name,industry,symbol,series,isin code\n"
        "Reliance Industries Ltd.,Oil & Gas,RELIANCE,EQ,INE002A01018\n",
        encoding="utf-8",
    )
    res = import_group(
        csv_path=csv_file,
        group_name="lower_group",
        groups_dir=tmp_project / "config" / "groups",
        data_dir=tmp_project / "data",
    )
    assert res.mapped_count == 1
    assert "NSE:EQ:RELIANCE" in res.mapped_members


def test_group_import_cli_refusal_and_override(tmp_project: Any) -> None:
    _make_dummy_master(tmp_project)
    csv_file = tmp_project / "unmapped_cli.csv"
    csv_file.write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance,Oil,RELIANCE,EQ,INE002A01018\n"
        "Unknown1,None,UNKNOWN1,EQ,DUMMYISIN1\n"
        "Unknown2,None,UNKNOWN2,EQ,DUMMYISIN2\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    # Refusal without flag
    res_fail = runner.invoke(app, ["groups", "import", str(csv_file), "--name", "fail_cli"])
    assert res_fail.exit_code != 0
    assert "exceeds maximum allowed" in res_fail.output

    # Acceptance with flag
    res_ok = runner.invoke(
        app, ["groups", "import", str(csv_file), "--name", "ok_cli", "--accept-unmapped"]
    )
    assert res_ok.exit_code == 0
    assert "Imported group 'ok_cli'" in res_ok.output
    assert "Unmapped: 2" in res_ok.output
