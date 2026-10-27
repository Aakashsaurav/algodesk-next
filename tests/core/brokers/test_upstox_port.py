"""Tests for Upstox doctor redirect port check.

Verifies binding behavior on free port, exclusive listener, and wildcard listener.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock

import pytest
from plugins.brokers.upstox.config_schema import UpstoxConfig
from plugins.brokers.upstox.plugin import UpstoxBrokerPlugin

from algodesk.api import PluginContext, PluginManifest


@pytest.fixture
def upstox_manifest() -> PluginManifest:
    return PluginManifest(
        name="upstox",
        kind="broker",  # type: ignore[arg-type]
        version="0.1.0",
        api_version=1,
        entry="plugin:UpstoxBrokerPlugin",
        capabilities={
            "history": True,
            "stream": True,
            "realtime": True,
            "live_suitable": True,
            "official_api": True,
            "delay_seconds": 0,
        },
    )


@pytest.fixture
def fake_context() -> MagicMock:
    ctx = MagicMock(spec=PluginContext)
    ctx.params = MagicMock()
    ctx.params.get.return_value = None
    ctx.secrets = MagicMock()
    ctx.secrets.get.return_value = None
    return ctx


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_redirect_port_free(upstox_manifest: PluginManifest, fake_context: MagicMock) -> None:
    port = _find_free_port()
    cfg = UpstoxConfig(redirect_uri=f"http://127.0.0.1:{port}/")
    plugin = UpstoxBrokerPlugin(upstox_manifest, config=cfg.model_dump(), ctx=fake_context)
    res = plugin._check_redirect_port()
    assert res.ok is True
    assert f"Redirect port {port} is free" in res.message


def test_redirect_port_held_by_exclusive_listener(
    upstox_manifest: PluginManifest, fake_context: MagicMock
) -> None:
    port = _find_free_port()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)

        cfg = UpstoxConfig(redirect_uri=f"http://127.0.0.1:{port}/")
        plugin = UpstoxBrokerPlugin(upstox_manifest, config=cfg.model_dump(), ctx=fake_context)
        res = plugin._check_redirect_port()
        assert res.ok is False
        assert f"Redirect port {port} is in use" in res.message
    finally:
        listener.close()


def test_redirect_port_held_on_wildcard_address_only(
    upstox_manifest: PluginManifest, fake_context: MagicMock
) -> None:
    port = _find_free_port()
    wildcard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        wildcard.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            wildcard.bind(("0.0.0.0", port))
            wildcard.listen(1)
        except OSError:
            pytest.skip("Unable to bind to wildcard address in this environment")

        # The redirect_uri specifies 127.0.0.1; on macOS with SO_REUSEADDR,
        # binding 127.0.0.1 succeeds even with 0.0.0.0 listening.
        cfg = UpstoxConfig(redirect_uri=f"http://127.0.0.1:{port}/")
        plugin = UpstoxBrokerPlugin(upstox_manifest, config=cfg.model_dump(), ctx=fake_context)
        res = plugin._check_redirect_port()
        assert res.ok is True
        assert f"Redirect port {port} is free" in res.message
    finally:
        wildcard.close()
