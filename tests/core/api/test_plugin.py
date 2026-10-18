"""Tests for algodesk.api.plugin — Plugin protocol and PluginBase class."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from algodesk.api.checks import CheckResult, DoctorCheck, Severity
from algodesk.api.errors import NotSupported
from algodesk.api.kinds import API_VERSION, Mode, PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.plugin import Plugin, PluginBase
from algodesk.api.services import (
    ParamValue,
    PluginContext,
)


class DummyParamReader:
    def get(self, name: str, scope: str | None = None) -> ParamValue:
        return 0


class DummyJournalWriter:
    def append(
        self,
        kind: str,
        payload: Mapping[str, object],
        *,
        durable: bool = False,
    ) -> int:
        return 1


class DummySecretReader:
    def get(self, name: str) -> str:
        return "val"


def _make_context() -> PluginContext:
    return PluginContext(
        mode=Mode.RESEARCH,
        run_id="run-test",
        params=DummyParamReader(),
        journal=DummyJournalWriter(),
        secrets=DummySecretReader(),
    )


def _make_manifest(
    *,
    name: str = "mock_broker",
    kind: PluginKind = PluginKind.BROKER,
    capabilities: dict[str, bool] | None = None,
) -> PluginManifest:
    return PluginManifest(
        name=name,
        kind=kind,
        version="1.0.0",
        api_version=API_VERSION,
        entry="mock.plugin:MockBroker",
        capabilities=capabilities or {"place_order": True, "cancel_order": False},
        modes_allowed=[Mode.RESEARCH, Mode.BACKTEST],
    )


class SampleCustomPlugin(PluginBase):
    """Subclass of PluginBase representing an entry class."""

    def doctor_checks(self) -> Sequence[DoctorCheck]:
        return (
            DoctorCheck(
                id=f"{self.manifest.name}.ping",
                severity=Severity.CRITICAL,
                run=lambda: CheckResult(ok=True, message="ready"),
            ),
        )


def test_plugin_base_stores_attributes() -> None:
    """PluginBase constructor stores manifest, config, and ctx."""
    manifest = _make_manifest()
    config = {"timeout_ms": 500}
    ctx = _make_context()

    plugin = PluginBase(manifest=manifest, config=config, ctx=ctx)
    assert plugin.manifest is manifest
    assert plugin.config == {"timeout_ms": 500}
    assert plugin.ctx is ctx


def test_plugin_protocol_isinstance_base() -> None:
    """PluginBase is recognised by isinstance(..., Plugin)."""
    manifest = _make_manifest()
    plugin = PluginBase(manifest=manifest, config={}, ctx=_make_context())
    assert isinstance(plugin, Plugin)


def test_plugin_protocol_isinstance_subclass() -> None:
    """PluginBase subclass is recognised by isinstance(..., Plugin)."""
    manifest = _make_manifest()
    plugin = SampleCustomPlugin(manifest=manifest, config={}, ctx=_make_context())
    assert isinstance(plugin, Plugin)


def test_plugin_protocol_rejects_non_conforming_objects() -> None:
    """Object lacking required attributes is not a Plugin."""

    class IncompletePlugin:
        manifest = _make_manifest()

    assert not isinstance(IncompletePlugin(), Plugin)


def test_plugin_base_supports_delegates_to_manifest() -> None:
    """supports() returns True when capability is true, False otherwise."""
    manifest = _make_manifest(capabilities={"streaming": True, "historical": False})
    plugin = PluginBase(manifest=manifest, config={}, ctx=_make_context())

    assert plugin.supports("streaming") is True
    assert plugin.supports("historical") is False
    assert plugin.supports("unregistered_cap") is False


def test_plugin_base_require_succeeds_when_supported() -> None:
    """require() does not raise for enabled capability."""
    manifest = _make_manifest(capabilities={"streaming": True})
    plugin = PluginBase(manifest=manifest, config={}, ctx=_make_context())

    # Should not raise
    plugin.require("streaming")


def test_plugin_base_require_raises_not_supported_when_disabled() -> None:
    """require() raises NotSupported when capability is explicitly False."""
    manifest = _make_manifest(capabilities={"streaming": False})
    plugin = PluginBase(manifest=manifest, config={}, ctx=_make_context())

    with pytest.raises(NotSupported) as exc_info:
        plugin.require("streaming")

    err = exc_info.value
    assert "streaming" in err.what
    assert manifest.name in err.where
    assert "capabilities" in err.fix


def test_plugin_base_require_raises_not_supported_when_missing() -> None:
    """require() raises NotSupported when capability is not declared."""
    manifest = _make_manifest(capabilities={"streaming": True})
    plugin = PluginBase(manifest=manifest, config={}, ctx=_make_context())

    with pytest.raises(NotSupported) as exc_info:
        plugin.require("unknown_capability")

    err = exc_info.value
    assert "unknown_capability" in err.what
    assert manifest.name in err.where
    assert len(err.fix) > 0


def test_plugin_base_doctor_checks_returns_empty_tuple() -> None:
    """PluginBase default doctor_checks() returns empty tuple."""
    plugin = PluginBase(manifest=_make_manifest(), config={}, ctx=_make_context())
    checks = plugin.doctor_checks()
    assert checks == ()


def test_plugin_subclass_custom_doctor_checks() -> None:
    """Subclass can provide custom doctor checks."""
    plugin = SampleCustomPlugin(manifest=_make_manifest(), config={}, ctx=_make_context())
    checks = plugin.doctor_checks()
    assert len(checks) == 1
    assert checks[0].id == "mock_broker.ping"
    assert checks[0].severity is Severity.CRITICAL
    res = checks[0].run()
    assert res.ok is True
