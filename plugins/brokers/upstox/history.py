"""Upstox historical and intraday candle API client returning Bar sequences."""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from algodesk.api import (
    AlgoDeskError,
    AuthError,
    Bar,
    ConfigError,
    DataError,
    PluginContext,
    RateLimitError,
    SeriesKey,
)

from .instruments import UpstoxInstrumentManager


class UpstoxHistoryClient:
    """Fetches historical and intraday candlestick bars from Upstox API V3."""

    def __init__(
        self,
        instruments: UpstoxInstrumentManager,
        ctx: PluginContext | None = None,
        *,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._instruments = instruments
        self._ctx = ctx
        self._transport = http_transport

    def _param(self, name: str, default: Any) -> Any:
        if self._ctx is not None and self._ctx.params is not None:
            try:
                val = self._ctx.params.get(name)
                return val if val is not None else default
            except Exception:
                return default
        return default

    def _client(self) -> httpx.Client:
        timeout = float(self._param("upstox.request_timeout_s", 15.0))
        return httpx.Client(transport=self._transport, timeout=timeout)

    def _parse_timeframe(self, tf: str) -> tuple[str, str]:
        tf_lower = tf.lower()
        if tf_lower == "1d":
            return "days", "1"
        if tf_lower.endswith("m"):
            mins = tf_lower[:-1]
            if mins in ("1", "5", "15", "30", "60"):
                return "minutes", mins
        if tf_lower.endswith("h"):
            hrs = tf_lower[:-1]
            return "hours", hrs

        raise ConfigError(
            what=f"Unsupported Upstox timeframe interval '{tf}'",
            where="UpstoxHistoryClient.history",
            fix="Use supported intervals: '1m', '5m', '15m', '30m', '1h', '1d'",
        )

    def history(
        self,
        key: SeriesKey,
        start: datetime,
        end: datetime,
        token: str | None = None,
    ) -> Iterable[Bar]:
        """Fetch historical candles from Upstox and convert to canonical Bar objects."""
        inst = self._instruments.resolve(key.instrument)
        upstox_key = inst.broker_keys.get("upstox")
        if not upstox_key:
            raise ConfigError(
                what=f"Instrument {key.instrument} has no mapped Upstox key",
                where="UpstoxHistoryClient.history",
                fix="Ensure instrument master mapping includes 'upstox' broker key",
            )

        unit, interval = self._parse_timeframe(key.timeframe)
        from_str = start.strftime("%Y-%m-%d")
        to_str = end.strftime("%Y-%m-%d")

        safe_key = urllib.parse.quote(upstox_key, safe="")
        url = f"https://api.upstox.com/v3/historical-candle/{safe_key}/{unit}/{interval}/{to_str}/{from_str}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        with self._client() as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 429:
                raise RateLimitError(
                    what="Upstox API rate limit exceeded (HTTP 429)",
                    where="UpstoxHistoryClient.history",
                    fix="Back off requests or adjust rate limit in config/params/upstox.toml",
                )
            if resp.status_code in (401, 403):
                raise AuthError(
                    what=(
                        f"Upstox API authentication/authorization failed with "
                        f"HTTP {resp.status_code}"
                    ),
                    where="UpstoxHistoryClient.history",
                    fix="Refresh analytics/oauth token using 'algodesk login upstox'",
                )
            if resp.status_code in (400, 404):
                raise DataError(
                    what=(
                        f"Candle history request failed: query rejected with "
                        f"HTTP {resp.status_code}: {resp.text}"
                    ),
                    where="UpstoxHistoryClient.history",
                    fix="Verify instrument key, timeframe unit, and date range",
                )
            if resp.status_code != 200:
                raise AlgoDeskError(
                    what=f"Candle history request failed with HTTP {resp.status_code}",
                    where="UpstoxHistoryClient.history",
                    fix="Verify date range, instrument key, and Upstox authentication status",
                )
            payload = resp.json()

        if payload.get("status") != "success" or "data" not in payload:
            raise AlgoDeskError(
                what="Malformed candle history response payload from Upstox",
                where="UpstoxHistoryClient.history",
                fix="Inspect Upstox API response envelope",
            )

        raw_candles = payload.get("data", {}).get("candles", [])
        if not raw_candles:
            return []

        bars: list[Bar] = []
        for c in raw_candles:
            if not isinstance(c, (list, tuple)) or len(c) < 6:
                continue
            ts_str, open_p, high_p, low_p, close_p, vol = c[0], c[1], c[2], c[3], c[4], c[5]
            try:
                dt = datetime.fromisoformat(ts_str)
                dt_utc = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
            except Exception:
                continue

            delta = timedelta(days=1) if unit == "days" else timedelta(minutes=int(interval))
            bars.append(
                Bar(
                    instrument=key.instrument,
                    timeframe=key.timeframe,
                    start_ts=dt_utc,
                    end_ts=dt_utc + delta,
                    open=float(open_p),
                    high=float(high_p),
                    low=float(low_p),
                    close=float(close_p),
                    volume=int(vol),
                    complete=True,
                )
            )

        # Upstox returns newest candles first; sort ascending chronologically
        bars.sort(key=lambda b: b.start_ts)
        return bars
