"""Sliding-window rate limiter keyed by (source, endpoint) with backoff and jitter."""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from algodesk.api.manifest import PluginManifest
from algodesk.api.protocols import Clock, SystemClock
from algodesk.api.services import JournalWriter, ParamReader


@dataclass
class _EndpointRateState:
    calls: deque[float] = field(default_factory=deque)
    paused_until: float = 0.0
    consecutive_errors: int = 0


class RateLimiter:
    """Sliding window rate limiter across second, minute, and 30-minute horizons.

    Keyed by (source, endpoint). Thread-safe for multi-worker parallel execution.
    """

    __slots__ = (
        "_clock",
        "_journal",
        "_lock",
        "_manifests",
        "_params",
        "_sleep_fn",
        "_states",
    )

    def __init__(
        self,
        params: ParamReader | None = None,
        manifests: Mapping[str, PluginManifest] | None = None,
        journal: JournalWriter | None = None,
        clock: Clock | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._params = params
        self._manifests = dict(manifests) if manifests else {}
        self._journal = journal
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep_fn: Callable[[float], None] = sleep_fn if sleep_fn is not None else time.sleep
        self._lock = threading.Lock()
        self._states: dict[tuple[str, str], _EndpointRateState] = {}

    def _get_state(self, source: str, endpoint: str) -> _EndpointRateState:
        key = (source, endpoint)
        if key not in self._states:
            self._states[key] = _EndpointRateState()
        return self._states[key]

    def get_limits(self, source: str, endpoint: str) -> tuple[int, int, int]:
        """Resolve (req_per_s, req_per_min, req_per_30min) from registry or manifest."""
        limit_s: float | None = None
        limit_m: float | None = None
        limit_30m: float | None = None

        # 1. Registry parameters
        if self._params is not None:
            try:
                val = self._params.get(f"source.{source}.{endpoint}.requests_per_second")
                if val is not None:
                    limit_s = float(val)
            except Exception:
                pass
            try:
                val = self._params.get(f"source.{source}.{endpoint}.requests_per_minute")
                if val is not None:
                    limit_m = float(val)
            except Exception:
                pass
            try:
                val = self._params.get(f"source.{source}.{endpoint}.requests_per_30min")
                if val is not None:
                    limit_30m = float(val)
            except Exception:
                pass

        # 2. Manifest fallback
        manifest = self._manifests.get(source)
        if manifest is not None and hasattr(manifest, "limits") and manifest.limits:
            ep_limits = manifest.limits.get(endpoint, {})
            if isinstance(ep_limits, dict):
                if limit_s is None and "requests_per_second" in ep_limits:
                    limit_s = float(ep_limits["requests_per_second"])
                if limit_m is None and "requests_per_minute" in ep_limits:
                    limit_m = float(ep_limits["requests_per_minute"])
                if limit_30m is None and "requests_per_30min" in ep_limits:
                    limit_30m = float(ep_limits["requests_per_30min"])

        # Fallbacks: if still unset, safe generous defaults
        res_s = int(limit_s) if limit_s is not None else 1000000
        res_m = int(limit_m) if limit_m is not None else 1000000
        res_30m = int(limit_30m) if limit_30m is not None else 1000000
        return (max(1, res_s), max(1, res_m), max(1, res_30m))

    def _get_backoff_params(self, source: str, endpoint: str) -> tuple[float, float]:
        base_s: float | None = None
        jitter_s: float | None = None

        if self._params is not None:
            try:
                val = self._params.get(f"source.{source}.{endpoint}.backoff_base_s")
                if val is not None:
                    base_s = float(val)
            except Exception:
                pass
            if base_s is None:
                try:
                    val = self._params.get("data.rate_backoff_base_s")
                    if val is not None:
                        base_s = float(val)
                except Exception:
                    pass

            try:
                val = self._params.get(f"source.{source}.{endpoint}.backoff_jitter_s")
                if val is not None:
                    jitter_s = float(val)
            except Exception:
                pass
            if jitter_s is None:
                try:
                    val = self._params.get("data.rate_backoff_jitter_s")
                    if val is not None:
                        jitter_s = float(val)
                except Exception:
                    pass

        return (base_s if base_s is not None else 1.0, jitter_s if jitter_s is not None else 0.5)

    def is_paused(self, source: str, endpoint: str) -> bool:
        """Check whether endpoint is currently in a 429/rate-error backoff pause."""
        with self._lock:
            state = self._get_state(source, endpoint)
            now = self._clock.monotonic_ns() / 1e9
            return now < state.paused_until

    def acquire(self, source: str, endpoint: str) -> None:
        """Acquire rate limiter slot across 1s, 1m, and 30m windows, pausing if needed."""
        lim_s, lim_m, lim_30m = self.get_limits(source, endpoint)

        while True:
            with self._lock:
                state = self._get_state(source, endpoint)
                now = self._clock.monotonic_ns() / 1e9

                # Check pause state
                if now < state.paused_until:
                    wait_time = state.paused_until - now
                else:
                    # Purge calls older than 1800s
                    cutoff_30m = now - 1800.0
                    while state.calls and state.calls[0] < cutoff_30m:
                        state.calls.popleft()

                    wait_needed = 0.0

                    # 1-second window check
                    cutoff_1s = now - 1.0
                    calls_1s = [t for t in state.calls if t >= cutoff_1s]
                    if len(calls_1s) >= lim_s:
                        idx = len(calls_1s) - lim_s
                        wait_needed = max(wait_needed, (calls_1s[idx] + 1.0) - now)

                    # 60-second window check
                    cutoff_60s = now - 60.0
                    calls_60s = [t for t in state.calls if t >= cutoff_60s]
                    if len(calls_60s) >= lim_m:
                        idx = len(calls_60s) - lim_m
                        wait_needed = max(wait_needed, (calls_60s[idx] + 60.0) - now)

                    # 1800-second window check
                    if len(state.calls) >= lim_30m:
                        idx = len(state.calls) - lim_30m
                        wait_needed = max(wait_needed, (state.calls[idx] + 1800.0) - now)

                    if wait_needed <= 0.0:
                        state.calls.append(now)
                        return

                    wait_time = wait_needed

            self._sleep_fn(max(0.001, wait_time))

    def record_rate_error(
        self,
        source: str,
        endpoint: str,
        exc: Exception | None = None,
    ) -> float:
        """Pause all workers for this (source, endpoint) with exponential backoff and jitter."""
        base_s, jitter_s = self._get_backoff_params(source, endpoint)
        with self._lock:
            state = self._get_state(source, endpoint)
            state.consecutive_errors += 1
            exp = min(state.consecutive_errors - 1, 6)
            delay = float((base_s * (2**exp)) + random.uniform(0.0, jitter_s))
            now = self._clock.monotonic_ns() / 1e9
            state.paused_until = max(state.paused_until, now + delay)

            if self._journal is not None:
                self._journal.append(
                    kind="data.rate_limit_pause",
                    payload={
                        "source": source,
                        "endpoint": endpoint,
                        "consecutive_errors": state.consecutive_errors,
                        "delay_s": round(delay, 4),
                        "paused_until": round(state.paused_until, 4),
                        "error": str(exc) if exc else "Rate limit reached",
                    },
                )
            return delay

    def record_success(self, source: str, endpoint: str) -> None:
        """Reset consecutive error count after a successful API call."""
        with self._lock:
            state = self._get_state(source, endpoint)
            state.consecutive_errors = 0
