"""Telegram Notifier plugin implementation."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from algodesk.api import (
    CheckResult,
    DoctorCheck,
    Message,
    PluginBase,
    PluginContext,
    PluginManifest,
    Severity,
    resolve_secret_ref,
)

from .client import TelegramClient
from .config_schema import TelegramNotifierConfig

logger = logging.getLogger("algodesk.plugins.notifiers.telegram")


class TelegramNotifierPlugin(PluginBase):
    """Notifier plugin sending alerts and reports via Telegram Bot API."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, object] | None = None,
        ctx: PluginContext | Any = None,
        *,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(manifest, config or {}, ctx)
        self._cfg = TelegramNotifierConfig.model_validate(self.config)
        self._transport = http_transport
        self._client: TelegramClient | None = None
        self._init_client()

    def _param(self, name: str, default: Any) -> Any:
        if self.ctx is not None and getattr(self.ctx, "params", None) is not None:
            try:
                val = self.ctx.params.get(name)
                return getattr(val, "value", val) if val is not None else default
            except Exception:
                return default
        return default

    def _init_client(self) -> None:
        try:
            reader = getattr(self.ctx, "secrets", None)
            token = resolve_secret_ref(self._cfg.bot_token, reader)
            chat_id = resolve_secret_ref(self._cfg.chat_id, reader)
            if not token or not chat_id:
                return

            self._client = TelegramClient(
                bot_token=token,
                chat_id=chat_id,
                parse_mode=self._cfg.parse_mode,
                queue_max_size=int(self._param("telegram.queue_max_size", 1000)),
                retry_max_attempts=int(self._param("telegram.retry_max_attempts", 3)),
                retry_backoff_base_s=float(self._param("telegram.retry_backoff_base_s", 1.0)),
                retry_jitter_s=float(self._param("telegram.retry_jitter_s", 0.5)),
                rate_limit_messages_per_second=float(
                    self._param("telegram.rate_limit_messages_per_second", 1.0)
                ),
                circuit_breaker_failures=int(
                    self._param("telegram.circuit_breaker_failures", 5)
                ),
                circuit_breaker_reset_seconds=float(
                    self._param("telegram.circuit_breaker_reset_seconds", 60.0)
                ),
                timeout_seconds=float(self._param("telegram.timeout_seconds", 10.0)),
                http_transport=self._transport,
            )
        except Exception as exc:
            logger.debug("Could not initialize TelegramClient: %s", exc)
            self._client = None

    def send(self, message: Message) -> None:
        """Enqueue message for delivery. Never raises or blocks."""
        try:
            if self._client is None:
                self._init_client()
            if self._client is None:
                logger.debug("Telegram client not configured; message ignored: %s", message.title)
                return

            formatted = self._format_message(message)
            self._client.enqueue(formatted)
        except Exception as exc:
            logger.warning("Telegram send failed safely: %s", exc)

    def _format_message(self, msg: Message) -> str:
        icon = "ℹ️"
        if msg.severity.lower() == "critical":
            icon = "🚨"
        elif msg.severity.lower() == "warning":
            icon = "⚠️"

        title = f"{icon} <b>{msg.title}</b>\n" if msg.title else ""
        return f"{title}{msg.text}"

    def doctor_checks(self) -> Sequence[DoctorCheck]:
        return (
            DoctorCheck(
                id="telegram.reachable",
                severity=Severity.WARNING,
                run=self._check_reachable,
            ),
        )

    def _check_reachable(self) -> CheckResult:
        """Verify reachability and credentials against Bot API getMe endpoint."""
        reader = getattr(self.ctx, "secrets", None)
        try:
            token = resolve_secret_ref(self._cfg.bot_token, reader)
            chat_id = resolve_secret_ref(self._cfg.chat_id, reader)
        except Exception:
            return CheckResult(
                ok=True,
                message="Telegram bot token or chat ID not configured in secrets (notifications disabled)",
            )

        if not token or not chat_id:
            return CheckResult(
                ok=True,
                message="Telegram bot token or chat ID not configured (notifications disabled)",
            )
        if self._transport is None and ":" not in token:
            return CheckResult(
                ok=True,
                message="Telegram bot token or chat ID not configured (notifications disabled)",
            )

        url = f"https://api.telegram.org/bot{token}/getMe"
        timeout = float(self._param("telegram.timeout_seconds", 10.0))
        try:
            with httpx.Client(transport=self._transport, timeout=timeout) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    bot_name = data.get("result", {}).get("username", "bot")
                    return CheckResult(
                        ok=True,
                        message=f"Telegram Bot API reachable (@{bot_name})",
                    )
                return CheckResult(
                    ok=False,
                    severity=Severity.WARNING,
                    message=f"Telegram Bot API returned HTTP {resp.status_code}",
                    fix="Verify telegram_bot_token with 'algodesk telegram setup'",
                )
        except Exception as exc:
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message=f"Cannot reach Telegram Bot API: {exc}",
                fix="Check network connectivity or Telegram API availability",
            )
