"""Tests for reference plugin kind, protocol conformance, limiter params, and CLI."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from algodesk.api.conformance import kind_problems, reference_problems
from algodesk.api.errors import DataError
from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import ActionRecord
from algodesk.api.protocols import ReferenceSource
from algodesk.cli.app import app
from algodesk.data.actions_store import ActionsStore, CorporateActionsManager
from algodesk.data.limiter import RateLimiter
from algodesk.registry.scaffold import scaffold_plugin


class DummyReferenceSource:
    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest

    def corporate_actions(
        self,
        symbol_or_isin: str,
        start: date | None = None,
        end: date | None = None,
    ) -> Sequence[ActionRecord]:
        return [
            ActionRecord(
                ex_date=date(2024, 10, 28),
                type="bonus",
                ratio=1.0,
                source="dummy",
                symbol="RELIANCE",
            )
        ]


def test_reference_protocol_runtime_checkable() -> None:
    manifest = PluginManifest.model_validate(
        {
            "name": "dummy_ref",
            "kind": "reference",
            "version": "0.1.0",
            "api_version": 1,
            "entry": "dummy:DummyReferenceSource",
            "verified_on": "2026-11-03",
            "sources": ["https://example.com"],
            "capabilities": {"corporate_actions": True},
            "limits": {"corporate_actions": {"requests_per_second": 1}},
        }
    )
    src = DummyReferenceSource(manifest)
    assert isinstance(src, ReferenceSource)


def test_reference_conformance_success() -> None:
    manifest = PluginManifest.model_validate(
        {
            "name": "dummy_ref",
            "kind": "reference",
            "version": "0.1.0",
            "api_version": 1,
            "entry": "dummy:DummyReferenceSource",
            "verified_on": "2026-11-03",
            "sources": ["https://example.com"],
            "capabilities": {"corporate_actions": True},
            "limits": {"corporate_actions": {"requests_per_second": 1}},
        }
    )
    src = DummyReferenceSource(manifest)
    assert not reference_problems(src)
    assert not kind_problems(src)


def test_reference_conformance_missing_corporate_actions_callable() -> None:
    class BadSource:
        manifest = PluginManifest.model_validate(
            {
                "name": "bad_ref",
                "kind": "reference",
                "version": "0.1.0",
                "api_version": 1,
                "entry": "bad:BadSource",
                "verified_on": "2026-11-03",
                "sources": ["https://example.com"],
                "capabilities": {"corporate_actions": True},
                "limits": {"corporate_actions": {"requests_per_second": 1}},
            }
        )

    probs = reference_problems(BadSource())
    assert any("must implement callable 'corporate_actions()'" in p for p in probs)


def test_reference_conformance_missing_limits_endpoint() -> None:
    class MissingLimitsSource:
        manifest = PluginManifest.model_validate(
            {
                "name": "no_limits",
                "kind": "reference",
                "version": "0.1.0",
                "api_version": 1,
                "entry": "bad:MissingLimitsSource",
                "verified_on": "2026-11-03",
                "sources": ["https://example.com"],
                "capabilities": {"corporate_actions": True},
                "limits": {"other": {"requests_per_second": 1}},
            }
        )

        def corporate_actions(self, symbol_or_isin: str, *args: object) -> Sequence[ActionRecord]:
            return []

    probs = reference_problems(MissingLimitsSource())
    assert any("must define 'corporate_actions' endpoint" in p for p in probs)


def test_scaffold_creates_valid_reference_plugin(tmp_path: Path) -> None:
    scaffold_plugin(dest_dir=tmp_path, kind=PluginKind.REFERENCE, name="sample_reference")
    plugin_dir = tmp_path / "reference" / "sample_reference"
    assert (plugin_dir / "plugin.toml").is_file()
    assert (plugin_dir / "README.md").is_file()
    assert (plugin_dir / "plugin.py").is_file()


def test_rate_limiter_resolves_nse_and_bse_parameters() -> None:
    class MockParams:
        def get(self, key: str) -> object:
            defaults = {
                "source.nse.corporate_actions.requests_per_second": 1,
                "source.nse.corporate_actions.requests_per_minute": 30,
                "source.nse.corporate_actions.requests_per_30min": 600,
                "source.bse.corporate_actions.requests_per_second": 1,
            }
            return defaults.get(key)

    limiter = RateLimiter(params=MockParams())  # type: ignore[arg-type]
    lim_s, lim_m, lim_30m = limiter.get_limits("nse", "corporate_actions")
    assert lim_s == 1
    assert lim_m == 30
    assert lim_30m == 600


def test_corporate_actions_manager_resolves_all_sources(tmp_path: Path) -> None:
    store = ActionsStore(data_dir=tmp_path)
    s1 = MagicMock()
    s1.actions.return_value = []
    s2 = MagicMock()
    s2.corporate_actions.return_value = []

    mgr = CorporateActionsManager(
        store=store,
        sources={"upstox": s1, "nse": s2},
    )
    res_sources = mgr._resolve_sources("all")
    assert sorted(res_sources) == ["nse", "upstox"]


def test_corporate_actions_manager_fetches_from_corporate_actions_method(tmp_path: Path) -> None:
    store = ActionsStore(data_dir=tmp_path)
    ref_src = MagicMock()
    ref_src.corporate_actions.return_value = [
        ActionRecord(
            ex_date=date(2024, 10, 28),
            type="bonus",
            ratio=1.0,
            source="nse",
            symbol="RELIANCE",
        )
    ]
    # Ensure no .actions attribute on ref_src
    del ref_src.actions

    mgr = CorporateActionsManager(
        store=store,
        sources={"nse": ref_src},
    )
    res = mgr.refresh("RELIANCE", source="nse", force=True)
    assert res["actions_count"] == 1
    assert "NSE:RELIANCE" in res["refreshed"]


def test_corporate_actions_manager_unconfigured_source_raises_data_error(tmp_path: Path) -> None:
    store = ActionsStore(data_dir=tmp_path)
    mgr = CorporateActionsManager(store=store, sources={})
    with pytest.raises(DataError) as exc_info:
        mgr.refresh("RELIANCE", source="unknown_source")
    assert "not configured" in exc_info.value.what


def test_cli_actions_refresh_bse_raises_blocked_error(tmp_project: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["--root", str(tmp_project), "data", "actions", "refresh", "RELIANCE", "--source", "bse"],
    )
    assert res.exit_code != 0
    assert "blocked by exchange bot protection" in res.output


def test_cli_actions_compare_empty_reports_clean(tmp_project: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "actions", "compare", "TCS"])
    assert res.exit_code == 0
    assert "No corporate actions found" in res.output


def test_cli_actions_compare_shows_table(tmp_project: Path) -> None:
    data_dir = tmp_project / "data"
    store = ActionsStore(data_dir=data_dir)
    r1 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="nse",
        symbol="RELIANCE",
        exchange="NSE",
    )
    r2 = ActionRecord(
        ex_date=date(2024, 10, 28),
        type="bonus",
        ratio=1.0,
        ratio_new=1.0,
        ratio_old=1.0,
        source="yfinance",
        symbol="RELIANCE",
        exchange="NSE",
    )
    store.write("NSE", "RELIANCE", [r1, r2])

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(tmp_project), "data", "actions", "compare", "RELIANCE"])
    assert res.exit_code == 0
    out = res.output
    assert "RELIANCE" in out
    assert "2024-10-28" in out
    assert "bonus" in out
    assert "nse" in out
    assert "yfinance" in out
    assert "confirmed" in out


def test_refresh_comma_separated_selector(tmp_project: Path) -> None:
    data_dir = tmp_project / "data"
    store = ActionsStore(data_dir=data_dir)
    mock_source = MagicMock()
    mock_source.actions.return_value = [
        ActionRecord(
            ex_date=date(2024, 10, 28),
            type="bonus",
            ratio=1.0,
            source="upstox",
            symbol="RELIANCE",
        )
    ]
    mgr = CorporateActionsManager(
        store,
        {"upstox": mock_source},
        params={"data.actions_ttl_hours": "0"},
    )
    res = mgr.refresh("RELIANCE,TCS,INFY", source="upstox", force=True)
    assert len(res["refreshed"]) == 3
    assert set(res["refreshed"]) == {"NSE:RELIANCE", "NSE:TCS", "NSE:INFY"}
