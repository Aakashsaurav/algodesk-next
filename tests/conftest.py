"""Pytest test suite configuration and global fixtures for AlgoDesk Next."""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any

import pytest

_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex
_CURRENT_TEST_IS_LIVE = False


def _is_loopback(host: str) -> bool:
    clean_host = host.strip().lower()
    if clean_host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", ""):
        return True
    try:
        ip = ipaddress.ip_address(clean_host)
        return ip.is_loopback or ip.is_unspecified
    except ValueError:
        return False


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    if os.environ.get("ALGODESK_LIVE_TESTS") == "1" or _CURRENT_TEST_IS_LIVE:
        return _ORIGINAL_CONNECT(self, address)

    # Allow AF_UNIX sockets
    if getattr(self, "family", None) == getattr(socket, "AF_UNIX", -1):
        return _ORIGINAL_CONNECT(self, address)

    # Address is usually (host, port) or (host, port, flowinfo, scopeid)
    if isinstance(address, tuple) and len(address) >= 1:
        host = str(address[0])
        if _is_loopback(host):
            return _ORIGINAL_CONNECT(self, address)

    try:
        self.close()
    except Exception:
        pass
    raise RuntimeError(
        f"Network connection to {address} blocked during tests! "
        "Tests must not touch external network unless marked @pytest.mark.live "
        "or ALGODESK_LIVE_TESTS=1 is set."
    )


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if os.environ.get("ALGODESK_LIVE_TESTS") == "1" or _CURRENT_TEST_IS_LIVE:
        return _ORIGINAL_CONNECT_EX(self, address)

    if getattr(self, "family", None) == getattr(socket, "AF_UNIX", -1):
        return _ORIGINAL_CONNECT_EX(self, address)

    if isinstance(address, tuple) and len(address) >= 1:
        host = str(address[0])
        if _is_loopback(host):
            return _ORIGINAL_CONNECT_EX(self, address)

    try:
        self.close()
    except Exception:
        pass
    raise RuntimeError(
        f"Network connection (connect_ex) to {address} blocked during tests! "
        "Tests must not touch external network unless marked @pytest.mark.live "
        "or ALGODESK_LIVE_TESTS=1 is set."
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    socket.socket.connect = _guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]


def pytest_runtest_setup(item: pytest.Item) -> None:
    global _CURRENT_TEST_IS_LIVE
    _CURRENT_TEST_IS_LIVE = (
        os.environ.get("ALGODESK_LIVE_TESTS") == "1" or item.get_closest_marker("live") is not None
    )


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    global _CURRENT_TEST_IS_LIVE
    _CURRENT_TEST_IS_LIVE = False
