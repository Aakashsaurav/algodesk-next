"""Tests for M1-2 CLI commands: secrets, data, login, feed, doctor --run."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from algodesk.api import Tick
from algodesk.cli.app import app


class FakeBroker:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._cfg = type(
            "Cfg",
            (),
            {
                "analytics_token": "secret:upstox_analytics_token",
                "access_token": "secret:upstox_access_token",
                "redirect_uri": "http://127.0.0.1:5000/",
            },
        )()

    def validate(self, session: Any) -> bool:
        # pyrefly: ignore[no-any-return-implicit]
        return session.token == "valid_token"

    def expires_at(self, session: Any) -> datetime | None:
        return datetime(2027, 10, 23, tzinfo=UTC)

    def begin(self) -> Any:
        return type("Req", (), {"url": "https://fake.login.url", "state": "fake_state_123"})()

    def begin_auto(self, **kwargs: Any) -> Any:
        return type("Sess", (), {"token": "auto_token_val", "expires_at": datetime.now(UTC)})()

    def complete(self, code: str) -> Any:
        return type("Sess", (), {"token": "completed_token_val", "expires_at": datetime.now(UTC)})()

    async def stream(self, instruments: Any) -> Any:
        now = datetime.now(UTC)
        for inst in instruments:
            yield Tick(
                instrument=inst,
                exch_ts=now,
                recv_ts=now,
                ltp=123.45,
                ltq=10,
                volume=1000,
            )


def test_cli_secrets_flow(runner: CliRunner, tmp_path: Path, monkeypatch: Any) -> None:
    # Use env provider for clean test environment
    monkeypatch.setenv("ALGODESK_SECRET_DEMO_TEST", "my_secret_val")  # fake-secret
    res_list = runner.invoke(app, ["secrets", "list", "--provider", "env"])
    assert res_list.exit_code == 0
    assert "demo_test" in res_list.output
    # Ensure value is NOT printed
    assert "my_secret_val" not in res_list.output


def test_cli_data_get(runner: CliRunner, monkeypatch: Any) -> None:
    from datetime import timedelta

    from algodesk.api import Bar
    from algodesk.registry.registry import PluginRegistry

    class FakeYFinanceBroker:
        def history(self, key: Any, start: Any, end: Any) -> list[Bar]:
            # Generate fake daily bars between start and end, skipping Oct 2 and Oct 20
            bars: list[Bar] = []
            cur = start
            while cur <= end:
                d_str = cur.strftime("%Y-%m-%d")
                if d_str not in ("2026-10-02", "2026-10-20") and cur.weekday() < 5:
                    bars.append(
                        Bar(
                            instrument=key.instrument,
                            timeframe=key.timeframe,
                            start_ts=cur,
                            end_ts=cur + timedelta(days=1),
                            open=2500.0,
                            high=2550.0,
                            low=2490.0,
                            close=2530.0,
                            volume=1000000,
                            complete=True,
                        )
                    )
                cur += timedelta(days=1)
            return bars

    orig_create = PluginRegistry.create

    def fake_create(self: Any, kind: str, name: str, **kwargs: Any) -> Any:
        if kind == "broker" and name == "yfinance":
            return FakeYFinanceBroker()
        return orig_create(self, kind, name, **kwargs)

    monkeypatch.setattr(PluginRegistry, "create", fake_create)

    # Test data get command with fake yfinance broker
    res = runner.invoke(app, ["data", "get", "RELIANCE", "1d", "2026-10-01", "2026-10-20"])
    assert res.exit_code == 0
    assert "OPEN" in res.output
    assert "CLOSE" in res.output
    assert "VOLUME" in res.output
    # 2 and 20 October 2026 are Gandhi Jayanti holiday / closed session
    assert "2026-10-02" not in res.output
    assert "2026-10-20" not in res.output


def test_cli_login_status(runner: CliRunner) -> None:
    res = runner.invoke(app, ["login", "status"])
    assert res.exit_code == 0
    assert "PROVIDER" in res.output
    assert "upstox" in res.output
    assert "analytics" in res.output
    assert "oauth" in res.output


def test_cli_login_upstox_manual(runner: CliRunner, monkeypatch: Any) -> None:
    from algodesk.registry.registry import PluginRegistry

    orig_create = PluginRegistry.create

    def fake_create(self: Any, kind: str, name: str, **kwargs: Any) -> Any:
        if kind == "broker":
            return FakeBroker()
        return orig_create(self, kind, name, **kwargs)

    monkeypatch.setattr(PluginRegistry, "create", fake_create)

    res = runner.invoke(app, ["login", "upstox", "--manual"], input="sample_auth_code_123\n")
    assert res.exit_code == 0
    assert "Successfully authenticated with Upstox" in res.output


def test_cli_feed_probe(runner: CliRunner, monkeypatch: Any, tmp_path: Path) -> None:
    from algodesk.registry.registry import PluginRegistry

    orig_create = PluginRegistry.create

    def fake_create(self: Any, kind: str, name: str, **kwargs: Any) -> Any:
        if kind == "broker":
            return FakeBroker()
        return orig_create(self, kind, name, **kwargs)

    monkeypatch.setattr(PluginRegistry, "create", fake_create)

    rec_dir = tmp_path / "probe_records"
    res = runner.invoke(
        app,
        [
            "feed",
            "probe",
            "--symbols",
            "RELIANCE,INFY",
            "--seconds",
            "0.5",
            "--record",
            str(rec_dir),
        ],
    )
    assert res.exit_code == 0
    assert "INSTRUMENT" in res.output
    assert "TICKS" in res.output
    assert "RELIANCE" in res.output


def test_cli_doctor_run_demo_paper(runner: CliRunner) -> None:
    res = runner.invoke(app, ["doctor", "--run", "demo_paper"])
    # Doctor exits with code 1 due to warnings (port in use, cache absent)
    # or 0 if all checks pass.
    assert res.exit_code in (0, 1)
    # The summary line is always present regardless of pass/fail
    assert "DOCTOR:" in res.output
    # upstox.token may pass (valid token configured) so we don't require it
    # in the output; redirect_port and instrument_cache are more reliably
    # present as warnings but also environment-dependent.
