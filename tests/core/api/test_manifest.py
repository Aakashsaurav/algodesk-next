"""Tests for algodesk.api.manifest — valid and invalid manifests, parsed from TOML."""

from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from algodesk.api.errors import ManifestError, NotSupported
from algodesk.api.kinds import Mode, PluginKind
from algodesk.api.manifest import PluginManifest, parse_manifest


def _toml(text: str) -> dict[str, object]:
    return tomllib.loads(text)


# ------------------------------------------------------------------
# Helper: one valid TOML manifest per kind
# ------------------------------------------------------------------

_VALID_TEMPLATES: dict[PluginKind, str] = {
    PluginKind.BROKER: """
        name = "test_broker"
        kind = "broker"
        version = "0.1.0"
        api_version = 1
        entry = "plugins.broker.test_broker:TestBroker"
        [capabilities]
        orders = true
    """,
    PluginKind.VENUE: """
        name = "test_venue"
        kind = "venue"
        version = "1.0.0"
        api_version = 1
        entry = "plugins.venue.test_venue:TestVenue"
        [capabilities]
        market_data = true
    """,
    PluginKind.FEES: """
        name = "test_fees"
        kind = "fees"
        version = "0.2.0"
        api_version = 1
        entry = "plugins.fees.test_fees:TestFees"
        [capabilities]
        compute = true
    """,
    PluginKind.INDICATOR: """
        name = "test_indicator"
        kind = "indicator"
        version = "1.0.0"
        api_version = 1
        entry = "plugins.indicator.test_ind:TestInd"
        [capabilities]
        calculate = true
    """,
    PluginKind.SCREEN: """
        name = "test_screen"
        kind = "screen"
        version = "0.1.0"
        api_version = 1
        entry = "plugins.screen.test_scr:TestScr"
        [capabilities]
        filter = true
    """,
    PluginKind.STRATEGY: """
        name = "test_strategy"
        kind = "strategy"
        version = "1.0.0"
        api_version = 1
        entry = "plugins.strategy.test_strat:TestStrat"
        [capabilities]
        on_bar = true
    """,
    PluginKind.RISK_RULE: """
        name = "test_risk_rule"
        kind = "risk_rule"
        version = "0.1.0"
        api_version = 1
        entry = "plugins.risk_rule.test_rr:TestRR"
        [capabilities]
        check = true
    """,
    PluginKind.NOTIFIER: """
        name = "test_notifier"
        kind = "notifier"
        version = "1.0.0"
        api_version = 1
        entry = "plugins.notifier.test_notif:TestNotif"
        [capabilities]
        send = true
    """,
    PluginKind.CALENDAR: """
        name = "test_calendar"
        kind = "calendar"
        version = "0.1.0"
        api_version = 1
        entry = "plugins.calendar.test_cal:TestCal"
        [capabilities]
        is_trading_day = true
    """,
    PluginKind.SECRETS: """
        name = "test_secrets"
        kind = "secrets"
        version = "0.1.0"
        api_version = 1
        entry = "plugins.secrets.test_sec:TestSec"
        [capabilities]
        get = true
    """,
    PluginKind.TRANSPORT: """
        name = "test_transport"
        kind = "transport"
        version = "0.1.0"
        api_version = 1
        entry = "plugins.transport.test_tr:TestTr"
        [capabilities]
        connect = true
    """,
    PluginKind.MODEL: """
        name = "test_model"
        kind = "model"
        version = "0.1.0"
        api_version = 1
        entry = "plugins.model.test_mod:TestMod"
        [capabilities]
        predict = true
    """,
    PluginKind.REPORT: """
        name = "test_report"
        kind = "report"
        version = "0.1.0"
        api_version = 1
        entry = "plugins.report.test_rep:TestRep"
        [capabilities]
        generate = true
    """,
    PluginKind.REFERENCE: """
        name = "test_reference"
        kind = "reference"
        version = "0.1.0"
        api_version = 1
        entry = "plugins.reference.test_ref:TestRef"
        verified_on = "2026-11-03"
        sources = ["https://example.com"]
        [capabilities]
        corporate_actions = true
        [limits.corporate_actions]
        requests_per_second = 1
    """,
}


# ------------------------------------------------------------------
# Valid manifests
# ------------------------------------------------------------------


@pytest.mark.parametrize("kind", PluginKind, ids=lambda k: k.value)
def test_valid_manifest_per_kind(kind: PluginKind) -> None:
    data = _toml(_VALID_TEMPLATES[kind])
    m = parse_manifest(data, f"test:{kind.value}")
    assert m.kind == kind
    assert isinstance(m, PluginManifest)


def test_defaults(self: object = None) -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    m = parse_manifest(data, "test")
    assert m.modes_allowed == [Mode.RESEARCH, Mode.BACKTEST, Mode.PAPER]
    assert m.order_capable is False
    assert m.limits == {}
    assert m.requires == {}
    assert m.config_schema is None
    assert m.verified_on is None
    assert m.sources == []


def test_manifest_is_frozen() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    m = parse_manifest(data, "test")
    with pytest.raises(ValidationError):
        m.name = "other"  # type: ignore[misc]


# ------------------------------------------------------------------
# supports / require
# ------------------------------------------------------------------


def test_supports_true() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    m = parse_manifest(data, "test")
    assert m.supports("orders") is True


def test_supports_false_missing() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    m = parse_manifest(data, "test")
    assert m.supports("nonexistent") is False


def test_require_raises_not_supported() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    m = parse_manifest(data, "test")
    with pytest.raises(NotSupported, match="streaming"):
        m.require("streaming")


def test_require_ok() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    m = parse_manifest(data, "test")
    m.require("orders")  # should not raise


# ------------------------------------------------------------------
# Invalid manifests (at least 10)
# ------------------------------------------------------------------


def test_bad_name_uppercase() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    data["name"] = "BadName"
    with pytest.raises(ManifestError, match="name"):
        parse_manifest(data, "test:bad_name")


def test_bad_name_too_short() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    data["name"] = "x"
    with pytest.raises(ManifestError, match="name"):
        parse_manifest(data, "test:too_short")


def test_bad_version_not_semver() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    data["version"] = "v1"
    with pytest.raises(ManifestError, match="semver"):
        parse_manifest(data, "test:bad_version")


def test_wrong_api_version() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    data["api_version"] = 99
    with pytest.raises(ManifestError, match="api_version"):
        parse_manifest(data, "test:wrong_api")


def test_empty_capabilities() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    # pyrefly: ignore[implicit-any-empty-container]
    data["capabilities"] = {}
    with pytest.raises(ManifestError, match="capabilities"):
        parse_manifest(data, "test:empty_caps")


def test_order_capable_wrong_modes() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    data["order_capable"] = True
    # default modes_allowed is [research, backtest, paper], not [live]
    with pytest.raises(ManifestError, match="order_capable"):
        parse_manifest(data, "test:order_modes")


def test_order_capable_correct() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    data["order_capable"] = True
    data["modes_allowed"] = ["live"]
    m = parse_manifest(data, "test:order_ok")
    assert m.order_capable is True
    assert m.modes_allowed == [Mode.LIVE]


def test_sources_without_verified_on() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    data["sources"] = ["https://docs.example.com"]
    with pytest.raises(ManifestError, match="verified_on"):
        parse_manifest(data, "test:sources_no_date")


def test_sources_with_verified_on_ok() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    data["sources"] = ["https://docs.example.com"]
    data["verified_on"] = "2026-10-17"
    m = parse_manifest(data, "test:sources_ok")
    assert m.sources == ["https://docs.example.com"]


def test_unknown_key() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    data["unknown_field"] = "surprise"
    with pytest.raises(ManifestError):
        parse_manifest(data, "test:unknown_key")


def test_bad_entry_format() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    data["entry"] = "no_colon_here"
    with pytest.raises(ManifestError, match="entry"):
        parse_manifest(data, "test:bad_entry")


def test_missing_required_field() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    del data["entry"]
    with pytest.raises(ManifestError):
        parse_manifest(data, "test:missing_field")


def test_manifest_error_has_source_in_where() -> None:
    data = _toml(_VALID_TEMPLATES[PluginKind.BROKER])
    data["api_version"] = 0
    with pytest.raises(ManifestError) as exc_info:
        parse_manifest(data, "plugins/broker/bad/plugin.toml")
    assert "plugins/broker/bad/plugin.toml" in exc_info.value.where


# ------------------------------------------------------------------
# Isolation: api modules import nothing from algodesk outside api
# ------------------------------------------------------------------


def test_api_imports_only_api() -> None:
    """api submodules must not import from algodesk.config, .params, etc."""
    import subprocess
    import sys

    code = (
        "import importlib, sys; "
        "mods = ['algodesk.api', 'algodesk.api.checks', 'algodesk.api.errors', "
        "'algodesk.api.kinds', 'algodesk.api.manifest', 'algodesk.api.plugin', "
        "'algodesk.api.scrub', 'algodesk.api.services']; "
        "[importlib.import_module(m) for m in mods]; "
        "non_api = [k for k in sys.modules "
        "if k.startswith('algodesk.') and not k.startswith('algodesk.api')]; "
        "assert non_api == [], f'api imported non-api algodesk modules: {non_api}'"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0, f"Import isolation failed: {res.stderr}"
