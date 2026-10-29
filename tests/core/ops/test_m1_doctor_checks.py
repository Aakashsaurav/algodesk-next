"""Tests for M1 operational doctor checks in ops/checks.py."""

from __future__ import annotations

import struct
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from algodesk.api.checks import Severity
from algodesk.ops.checks import (
    _resolve_max_clock_offset_ms,
    build_run_checks,
)
from algodesk.ops.runtime import open_runtime


def test_resolve_max_clock_offset_ms_default() -> None:
    with open_runtime(Path("."), ensure=False) as rt:
        offset = _resolve_max_clock_offset_ms(rt)
        assert offset == 500


def test_resolve_max_clock_offset_ms_from_params() -> None:
    with open_runtime(Path("."), ensure=False) as rt:
        mock_params = MagicMock()
        mock_params.get.return_value = 250
        rt_mock = replace(rt, params=mock_params)
        assert _resolve_max_clock_offset_ms(rt_mock) == 250


@pytest.mark.parametrize(
    ("offset_seconds", "limit_ms", "expected_ok"),
    [
        (0.010, 500, True),
        (0.050, 500, True),
        (0.200, 500, True),
        (0.499, 500, True),
        (0.500, 500, True),
        (0.501, 500, False),
        (0.800, 500, False),
        (1.500, 500, False),
        (0.005, 50, True),
        (0.060, 50, False),
        (0.000, 100, True),
        (0.099, 100, True),
        (0.101, 100, False),
        (0.250, 250, True),
        (0.251, 250, False),
    ],
)
def test_sntp_offset_check_boundaries(
    offset_seconds: float, limit_ms: int, expected_ok: bool
) -> None:
    with open_runtime(Path("."), ensure=False) as rt:
        mock_params = MagicMock()
        mock_params.get.return_value = limit_ms
        rt_mock = replace(rt, params=mock_params)

        NTP_DELTA = 2208988800
        now_ts = 1000.0
        pkt = bytearray(48)
        pkt[0] = 0x24
        t_remote = now_ts + offset_seconds
        sec = int(t_remote) + NTP_DELTA
        frac = int((t_remote - int(t_remote)) * 4294967296)
        struct.pack_into("!II", pkt, 32, sec, frac)
        struct.pack_into("!II", pkt, 40, sec, frac)

        mock_sock = MagicMock()
        mock_sock.__enter__.return_value = mock_sock
        mock_sock.recvfrom.return_value = (bytes(pkt), ("time.google.com", 123))

        with (
            patch("socket.socket", return_value=mock_sock),
            patch("time.time", return_value=now_ts),
        ):
            checks = build_run_checks(rt_mock, "demo_paper")
            sntp_check = next(c for c in checks if c.id == "clock.sntp_offset")
            res = sntp_check.run()
            assert res.ok is expected_ok


def test_sntp_offset_check_network_error_handles_safely() -> None:
    with open_runtime(Path("."), ensure=False) as rt:
        with patch("socket.socket", side_effect=OSError("Network is unreachable")):
            checks = build_run_checks(rt, "demo_paper")
            sntp_check = next(c for c in checks if c.id == "clock.sntp_offset")
            res = sntp_check.run()
            assert res.ok is True
            assert "skipped or offline" in res.message


@pytest.mark.parametrize(
    ("venue_plugin", "mode", "expected_ok"),
    [
        ("sim", "paper", True),
        ("sim", "backtest", True),
        ("upstox", "paper", False),
        ("zerodha", "paper", False),
        ("shoonya", "paper", False),
        ("interactive_brokers", "backtest", False),
    ],
)
def test_paper_safety_sim_venue_check(venue_plugin: str, mode: str, expected_ok: bool) -> None:
    with open_runtime(Path("."), ensure=False) as rt:
        with patch("algodesk.config.snapshot.build_effective") as mock_eff_fn:
            mock_eff = MagicMock()
            mock_eff.run.run.mode = mode
            mock_eff.run.execution_venue.plugin = venue_plugin
            mock_eff_fn.return_value = mock_eff

            checks = build_run_checks(rt, "demo_paper")
            safety_check = next(c for c in checks if c.id == "run.safety.sim_venue")
            res = safety_check.run()
            assert res.ok is expected_ok
            if not expected_ok:
                assert safety_check.severity == Severity.CRITICAL


def test_calendar_coverage_check_success() -> None:
    with open_runtime(Path("."), ensure=False) as rt:
        checks = build_run_checks(rt, "demo_paper")
        cal_check = next(c for c in checks if c.id == "run.calendars")
        res = cal_check.run()
        assert res.ok is True
        assert "cover today and next 30 days" in res.message


def test_calendar_coverage_check_insufficient() -> None:
    with open_runtime(Path("."), ensure=False) as rt:
        mock_cal = MagicMock()
        mock_cal.coverage_end = date.today() + timedelta(days=5)  # Less than 30 days
        with patch.object(rt.plugins, "create", return_value=mock_cal):
            checks = build_run_checks(rt, "demo_paper")
            cal_check = next(c for c in checks if c.id == "run.calendars")
            res = cal_check.run()
            assert res.ok is False
            assert "less than 30 days" in res.message.lower() or "< 30 days" in res.message


@pytest.mark.parametrize(
    ("status_code", "resp_body", "expected_ok"),
    [
        (200, {"ok": True, "result": {"username": "algo_bot"}}, True),
        (401, {"ok": False, "description": "Unauthorized"}, False),
        (404, {"ok": False, "description": "Not Found"}, False),
        (500, {"ok": False, "description": "Internal Server Error"}, False),
    ],
)
def test_telegram_doctor_check_http_statuses(
    status_code: int, resp_body: dict[str, object], expected_ok: bool
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=resp_body)

    mock_secrets = MagicMock()
    mock_secrets.get.side_effect = lambda k: "secret_val"

    with open_runtime(Path("."), ensure=False) as rt:
        orig_create = rt.plugins.create

        def fake_create(kind: str, name: str, **kwargs: object) -> object:
            if str(kind) == "secrets":
                return mock_secrets
            p = orig_create(kind, name, **kwargs)
            if hasattr(p, "_transport"):
                p._transport = httpx.MockTransport(handler)
            return p

        with patch.object(rt.plugins, "create", side_effect=fake_create):
            checks = build_run_checks(rt, "demo_paper")
            tg_check = next(c for c in checks if c.id == "telegram.reachable")
            res = tg_check.run()
            assert res.ok is expected_ok


def test_plugins_conformance_doctor_check_all_current(tmp_project: Path) -> None:
    """Doctor check plugins.conformance passes when all plugins have current records."""
    with open_runtime(tmp_project, ensure=False) as rt:
        checks = build_run_checks(rt, "demo_paper")
        conf_check = next(c for c in checks if c.id == "plugins.conformance")
        assert conf_check.severity == Severity.CRITICAL
        res = conf_check.run()
        assert res.ok is True
        assert "All" in res.message


def test_plugins_conformance_doctor_check_missing_or_stale(tmp_project: Path) -> None:
    """Doctor check plugins.conformance is CRITICAL when a plugin record is missing/stale."""
    with open_runtime(tmp_project, ensure=False) as rt:
        orig_status = rt.plugins.conformance_status

        def mock_status(k: Any, name: str) -> str:
            if name == "upstox":
                return "stale"
            return orig_status(k, name)

        with patch.object(rt.plugins, "conformance_status", side_effect=mock_status):
            checks = build_run_checks(rt, "demo_paper")
            conf_check = next(c for c in checks if c.id == "plugins.conformance")
            res = conf_check.run()
            assert res.ok is False
            assert "broker/upstox (stale)" in res.message
            assert "algodesk plugins test broker upstox" in res.fix
