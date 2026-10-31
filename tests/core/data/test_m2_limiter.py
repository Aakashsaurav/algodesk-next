"""Tests for M2-2 RateLimiter sliding windows, registry limits, backoff, and property invariants."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from algodesk.api.manifest import PluginManifest
from algodesk.api.protocols import Clock
from algodesk.data.limiter import RateLimiter


class FakeSimClock(Clock):
    """Deterministic simulated clock with millisecond precision."""

    def __init__(self, start_ns: int = 1_000_000_000_000) -> None:
        self._ns = start_ns
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return datetime.fromtimestamp(self._ns / 1e9, tz=UTC)

    def monotonic_ns(self) -> int:
        with self._lock:
            return self._ns

    def advance_s(self, seconds: float) -> None:
        with self._lock:
            self._ns += int(seconds * 1e9)


class FakeParamReader:
    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        self._values = dict(values) if values else {}

    def get(self, name: str) -> Any:
        return self._values.get(name)


class FakeJournalWriter:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        self.entries.append({"kind": kind, "payload": payload})


def test_rate_limiter_resolves_registry_and_manifest_limits() -> None:
    params = FakeParamReader(
        {
            "source.upstox.history.requests_per_second": 40.0,
            "source.upstox.history.requests_per_minute": 400.0,
            "source.upstox.history.requests_per_30min": 1600.0,
        }
    )
    manifest = PluginManifest(
        name="upstox",
        kind="broker",
        version="0.1.0",
        api_version=1,
        entry="fake:Plugin",
        capabilities={"history": True},
        limits={
            "history": {
                "requests_per_second": 50,
                "requests_per_minute": 500,
                "requests_per_30min": 2000,
            }
        },
    )
    limiter = RateLimiter(params=params, manifests={"upstox": manifest})

    s, m, m30 = limiter.get_limits("upstox", "history")
    # Registry overrides (80% of published)
    assert s == 40
    assert m == 400
    assert m30 == 1600


def test_rate_limiter_falls_back_to_manifest_when_registry_unset() -> None:
    manifest = PluginManifest(
        name="yfinance",
        kind="broker",
        version="0.1.0",
        api_version=1,
        entry="fake:Plugin",
        capabilities={"history": True},
        limits={
            "history": {
                "requests_per_second": 2,
                "requests_per_minute": 60,
                "requests_per_30min": 1000,
            }
        },
    )
    limiter = RateLimiter(params=None, manifests={"yfinance": manifest})
    s, m, m30 = limiter.get_limits("yfinance", "history")
    assert s == 2
    assert m == 60
    assert m30 == 1000


def test_rate_limiter_sliding_window_1s() -> None:
    clock = FakeSimClock()
    sleeps: list[float] = []

    def fake_sleep(dur: float) -> None:
        sleeps.append(dur)
        clock.advance_s(dur)

    params = FakeParamReader(
        {
            "source.test.ep.requests_per_second": 2.0,
            "source.test.ep.requests_per_minute": 100.0,
            "source.test.ep.requests_per_30min": 1000.0,
        }
    )
    limiter = RateLimiter(params=params, clock=clock, sleep_fn=fake_sleep)

    # 1st call
    limiter.acquire("test", "ep")
    assert len(sleeps) == 0

    # 2nd call
    limiter.acquire("test", "ep")
    assert len(sleeps) == 0

    # 3rd call should trigger sleep to clear the 1s window
    limiter.acquire("test", "ep")
    assert len(sleeps) == 1
    assert sleeps[0] >= 0.99


def test_rate_limiter_sliding_window_60s() -> None:
    clock = FakeSimClock()
    sleeps: list[float] = []

    def fake_sleep(dur: float) -> None:
        sleeps.append(dur)
        clock.advance_s(dur)

    params = FakeParamReader(
        {
            "source.test.ep.requests_per_second": 10.0,
            "source.test.ep.requests_per_minute": 3.0,
            "source.test.ep.requests_per_30min": 1000.0,
        }
    )
    limiter = RateLimiter(params=params, clock=clock, sleep_fn=fake_sleep)

    limiter.acquire("test", "ep")
    clock.advance_s(0.5)
    limiter.acquire("test", "ep")
    clock.advance_s(0.5)
    limiter.acquire("test", "ep")
    # All 3 acquired within 1 second without hitting 1s limit (10)
    assert len(sleeps) == 0

    # 4th call hits 60s limit (3)
    limiter.acquire("test", "ep")
    assert len(sleeps) == 1
    assert sleeps[0] >= 58.0


def test_rate_limiter_rate_error_exponential_backoff_and_pause() -> None:
    clock = FakeSimClock()
    journal = FakeJournalWriter()
    params = FakeParamReader(
        {
            "data.rate_backoff_base_s": 2.0,
            "data.rate_backoff_jitter_s": 0.0,
        }
    )
    limiter = RateLimiter(params=params, journal=journal, clock=clock)

    assert not limiter.is_paused("upstox", "history")

    # First error -> 2.0s pause
    d1 = limiter.record_rate_error("upstox", "history")
    assert d1 == pytest.approx(2.0, abs=0.01)
    assert limiter.is_paused("upstox", "history")
    assert len(journal.entries) == 1
    assert journal.entries[0]["kind"] == "data.rate_limit_pause"
    assert journal.entries[0]["payload"]["consecutive_errors"] == 1

    # Second consecutive error -> 4.0s pause
    d2 = limiter.record_rate_error("upstox", "history")
    assert d2 == pytest.approx(4.0, abs=0.01)
    assert len(journal.entries) == 2
    assert journal.entries[1]["payload"]["consecutive_errors"] == 2

    # Advance clock past paused_until
    clock.advance_s(10.0)
    assert not limiter.is_paused("upstox", "history")

    # Success resets consecutive error counter
    limiter.record_success("upstox", "history")
    d3 = limiter.record_rate_error("upstox", "history")
    assert d3 == pytest.approx(2.0, abs=0.01)


def test_rate_limiter_independent_keys() -> None:
    clock = FakeSimClock()
    params = FakeParamReader(
        {
            "source.upstox.history.requests_per_second": 1.0,
            "source.yfinance.history.requests_per_second": 1.0,
        }
    )
    limiter = RateLimiter(params=params, clock=clock)

    limiter.acquire("upstox", "history")
    # yfinance history is completely independent
    limiter.acquire("yfinance", "history")


def test_property_n_workers_never_exceed_any_window() -> None:
    """Property test: N parallel workers never exceed 1s, 60s, or 1800s windows."""
    clock = FakeSimClock()
    lock = threading.Lock()
    call_timestamps: list[float] = []

    def fake_sleep(dur: float) -> None:
        with lock:
            clock.advance_s(dur)

    limit_s = 5
    limit_m = 15
    limit_30m = 30
    params = FakeParamReader(
        {
            "source.prop.ep.requests_per_second": float(limit_s),
            "source.prop.ep.requests_per_minute": float(limit_m),
            "source.prop.ep.requests_per_30min": float(limit_30m),
        }
    )
    limiter = RateLimiter(params=params, clock=clock, sleep_fn=fake_sleep)

    total_requests = 40
    workers = 8

    def worker_loop(count: int) -> None:
        for _ in range(count):
            limiter.acquire("prop", "ep")
            with lock:
                t = clock.monotonic_ns() / 1e9
                call_timestamps.append(t)

    threads = [
        threading.Thread(target=worker_loop, args=(total_requests // workers,))
        for _ in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(call_timestamps) == total_requests
    call_timestamps.sort()

    # Invariant 1: For any timestamp t, number of calls in [t, t + 1.0) <= limit_s
    for _i, t in enumerate(call_timestamps):
        window_1s = [ts for ts in call_timestamps if t <= ts < t + 1.0]
        assert len(window_1s) <= limit_s, f"Exceeded 1s window limit: {len(window_1s)} > {limit_s}"

    # Invariant 2: For any timestamp t, number of calls in [t, t + 60.0) <= limit_m
    for _i, t in enumerate(call_timestamps):
        window_60s = [ts for ts in call_timestamps if t <= ts < t + 60.0]
        assert len(window_60s) <= limit_m, (
            f"Exceeded 60s window limit: {len(window_60s)} > {limit_m}"
        )

    # Invariant 3: For any timestamp t, number of calls in [t, t + 1800.0) <= limit_30m
    for _i, t in enumerate(call_timestamps):
        window_1800s = [ts for ts in call_timestamps if t <= ts < t + 1800.0]
        assert len(window_1800s) <= limit_30m, (
            f"Exceeded 30min window limit: {len(window_1800s)} > {limit_30m}"
        )
