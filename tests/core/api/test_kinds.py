"""Tests for algodesk.api.kinds — plugin kinds and run modes."""

from __future__ import annotations

from algodesk.api.kinds import API_VERSION, Mode, PluginKind

_EXPECTED_KINDS = [
    "broker",
    "venue",
    "fees",
    "indicator",
    "screen",
    "strategy",
    "risk_rule",
    "notifier",
    "calendar",
    "secrets",
    "transport",
    "model",
    "report",
]

_EXPECTED_MODES = ["research", "backtest", "paper", "live"]


def test_api_version_is_one() -> None:
    assert API_VERSION == 1


def test_plugin_kind_count() -> None:
    assert len(PluginKind) == 13


def test_plugin_kind_values() -> None:
    actual = sorted(m.value for m in PluginKind)
    assert actual == sorted(_EXPECTED_KINDS)


def test_mode_count() -> None:
    assert len(Mode) == 4


def test_mode_values() -> None:
    actual = [m.value for m in Mode]
    assert actual == _EXPECTED_MODES


def test_kinds_are_str() -> None:
    for k in PluginKind:
        assert isinstance(k, str)


def test_modes_are_str() -> None:
    for m in Mode:
        assert isinstance(m, str)
