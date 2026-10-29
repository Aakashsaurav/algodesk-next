"""Telegram Bot API HTTP client with bounded queue, worker thread, retries, and circuit breaker."""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger("algodesk.plugins.notifiers.telegram")


class TelegramClient:
    """Outbound Telegram Bot API client ensuring non-blocking operations."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        parse_mode: str = "HTML",
        queue_max_size: int | None = None,
        retry_max_attempts: int | None = None,
        retry_backoff_base_s: float | None = None,
        retry_jitter_s: float | None = None,
        rate_limit_messages_per_second: float | None = None,
        circuit_breaker_failures: int | None = None,
        circuit_breaker_reset_seconds: float | None = None,
        timeout_seconds: float | None = None,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._parse_mode = parse_mode
        self._retry_max_attempts = retry_max_attempts if retry_max_attempts is not None else 3
        self._retry_backoff_base_s = (
            retry_backoff_base_s if retry_backoff_base_s is not None else 1.0
        )
        self._retry_jitter_s = retry_jitter_s if retry_jitter_s is not None else 0.5
        rate_limit = (
            rate_limit_messages_per_second if rate_limit_messages_per_second is not None else 1.0
        )
        self._min_interval_s = 1.0 / rate_limit if rate_limit > 0 else 0.0
        self._circuit_breaker_failures = (
            circuit_breaker_failures if circuit_breaker_failures is not None else 5
        )
        self._circuit_breaker_reset_s = (
            circuit_breaker_reset_seconds if circuit_breaker_reset_seconds is not None else 60.0
        )
        self._timeout_s = timeout_seconds if timeout_seconds is not None else 10.0
        self._transport = http_transport
        q_size = queue_max_size if queue_max_size is not None else 1000
        self._queue: queue.Queue[str] = queue.Queue(maxsize=max(1, q_size))
        self._stop_event = threading.Event()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._last_send_mono = 0.0

        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="TelegramNotifierWorker",
        )
        self._worker_thread.start()

    def enqueue(self, text: str) -> bool:
        """Enqueue message for delivery. Never blocks or raises."""
        try:
            self._queue.put_nowait(text)
            return True
        except queue.Full:
            try:
                _ = self._queue.get_nowait()
                self._queue.put_nowait(text)
                return True
            except Exception:
                logger.warning("Telegram delivery queue full; dropped message")
                return False
        except Exception as exc:
            logger.warning("Failed to enqueue Telegram message: %s", exc)
            return False

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                text = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            self._rate_limit()
            if self._is_circuit_open():
                logger.warning("Circuit breaker open; discarding Telegram message")
                self._queue.task_done()
                continue

            success = self._send_with_retries(text)
            if success:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._circuit_breaker_failures:
                    self._circuit_open_until = time.monotonic() + self._circuit_breaker_reset_s
                    logger.error(
                        "Telegram circuit breaker tripped; suspended for %.1fs",
                        self._circuit_breaker_reset_s,
                    )

            self._queue.task_done()

    def _is_circuit_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_send_mono
        if elapsed < self._min_interval_s:
            time.sleep(self._min_interval_s - elapsed)
        self._last_send_mono = time.monotonic()

    def _send_request(self, text: str) -> tuple[int, bool]:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": self._parse_mode,
        }
        with httpx.Client(transport=self._transport, timeout=self._timeout_s) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 200:
                return 200, True
            logger.warning("Telegram API error %d: %s", resp.status_code, resp.text)
            return resp.status_code, False

    def send_direct(self, text: str) -> bool:
        """Synchronously send message via Bot API endpoint."""
        _, ok = self._send_request(text)
        return ok

    def _send_with_retries(self, text: str) -> bool:
        for attempt in range(self._retry_max_attempts):
            try:
                status_code, ok = self._send_request(text)
                if ok:
                    return True
                if status_code in (400, 401, 403, 404):
                    return False
            except Exception as exc:
                logger.warning("Telegram send attempt %d failed: %s", attempt + 1, exc)

            if attempt < self._retry_max_attempts - 1:
                backoff = (self._retry_backoff_base_s * (2**attempt)) + random.uniform(
                    0.0, self._retry_jitter_s
                )
                time.sleep(backoff)
        return False

    def get_updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        """Fetch bot updates to inspect incoming commands (for telegram setup)."""
        url = f"https://api.telegram.org/bot{self._bot_token}/getUpdates"
        params: dict[str, Any] = {}
        if offset is not None:
            params["offset"] = offset
        with httpx.Client(transport=self._transport, timeout=self._timeout_s) as client:
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                res = data.get("result", [])
                return res if isinstance(res, list) else []
            return []

    def close(self) -> None:
        """Signal background worker thread to stop."""
        self._stop_event.set()
