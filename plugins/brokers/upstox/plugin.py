"""Upstox broker plugin implementing DataSource and AuthFlow protocols."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import cast

import httpx

from algodesk.api import (
    AlgoDeskError,
    AuthSession,
    Bar,
    CheckResult,
    DoctorCheck,
    Instrument,
    InstrumentId,
    LoginRequest,
    MarketEvent,
    PluginBase,
    PluginContext,
    PluginManifest,
    SeriesKey,
    Severity,
    resolve_secret_ref,
)

from .auth import UpstoxAuthFlow
from .config_schema import UpstoxConfig
from .feed import UpstoxWebSocketFeed
from .history import UpstoxHistoryClient
from .instruments import UpstoxInstrumentManager


class UpstoxBrokerPlugin(PluginBase):
    """Upstox DataSource and AuthFlow broker plugin."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: UpstoxConfig | Mapping[str, object] | None = None,
        ctx: PluginContext | None = None,
        *,
        _http_transport: httpx.BaseTransport | None = None,
        _ws_url_override: str | None = None,
        _cache_dir: Path | None = None,
    ) -> None:
        if isinstance(config, UpstoxConfig):
            self._cfg = config
            raw_config: Mapping[str, object] = config.model_dump()
        elif isinstance(config, Mapping):
            self._cfg = UpstoxConfig.model_validate(config)
            raw_config = config
        else:
            self._cfg = UpstoxConfig()
            raw_config = self._cfg.model_dump()

        super().__init__(manifest, raw_config, cast(PluginContext, ctx))
        self._http_transport = _http_transport
        self._ws_url_override = _ws_url_override

        self._auth = UpstoxAuthFlow(self._cfg, self.ctx, http_transport=self._http_transport)
        self._instruments_mgr = UpstoxInstrumentManager(
            self.ctx,
            cache_dir=_cache_dir,
            http_transport=self._http_transport,
        )
        self._history_client = UpstoxHistoryClient(
            self._instruments_mgr,
            self.ctx,
            http_transport=self._http_transport,
        )
        self._feed = UpstoxWebSocketFeed(
            self._instruments_mgr,
            self.ctx,
            http_transport=self._http_transport,
        )
        self._session: AuthSession | None = None

    @property
    def auth(self) -> UpstoxAuthFlow:
        """Access the underlying AuthFlow handler."""
        return self._auth

    def begin(self) -> LoginRequest:
        return self._auth.begin()

    def begin_auto(self, *, timeout_s: float | None = None) -> AuthSession:
        self._session = self._auth.begin_auto(timeout_s=timeout_s)
        return self._session

    def complete(self, code: str) -> AuthSession:
        self._session = self._auth.complete(code)
        return self._session

    def use_token(self, token: str, expires: date | None = None) -> AuthSession:
        self._session = self._auth.use_token(token, expires)
        return self._session

    def validate(self, session: AuthSession) -> bool:
        return self._auth.validate(session)

    def expires_at(self, session: AuthSession) -> datetime | None:
        return self._auth.expires_at(session)

    def resolve_active_token(self) -> str | None:
        """Resolve highest priority active token from secrets according to auth_priority."""
        if self._session is not None and self._session.token:
            return self._session.token

        reader = self.ctx.secrets if self.ctx is not None else None
        priority = self._cfg.auth_priority
        for kind in priority:
            if kind == "analytics":
                try:
                    tok = resolve_secret_ref(self._cfg.analytics_token, reader)
                    if tok:
                        return tok
                except Exception:
                    pass
            elif kind == "oauth":
                try:
                    tok = resolve_secret_ref(self._cfg.access_token, reader)
                    if tok:
                        return tok
                except Exception:
                    pass
        return None

    def instruments(self) -> Iterable[Instrument]:
        """Return sequence of tradable instruments."""
        self.require("stream")
        all_insts = list(self._instruments_mgr.all_instruments())
        if not all_insts:
            try:
                all_insts = self._instruments_mgr.load_or_fetch()
            except Exception:
                all_insts: list[Instrument] = []
        return all_insts

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        """Fetch historical candlestick bars from Upstox API."""
        self.require("history")
        token = self.resolve_active_token()
        return self._history_client.history(key, start, end, token=token)

    async def stream(self, instruments: Sequence[InstrumentId]) -> AsyncIterator[MarketEvent]:
        """Stream real-time MarketEvents over WebSocket feed."""
        self.require("stream")
        token = self.resolve_active_token()
        if not token:
            raise AlgoDeskError(
                what="No valid Upstox authentication token found for streaming",
                where="UpstoxBrokerPlugin.stream",
                fix="Run 'algodesk login upstox' or configure an analytics token",
            )
        async for ev in self._feed.stream(
            instruments, token=token, ws_url_override=self._ws_url_override
        ):
            yield ev

    def doctor_checks(self) -> Sequence[DoctorCheck]:
        """Return diagnostic health checks for Upstox broker plugin."""
        return (
            DoctorCheck(
                id="upstox.token",
                severity=Severity.WARNING,
                run=self._check_token,
            ),
            DoctorCheck(
                id="upstox.redirect_port",
                severity=Severity.WARNING,
                run=self._check_redirect_port,
            ),
            DoctorCheck(
                id="upstox.instrument_cache",
                severity=Severity.WARNING,
                run=self._check_instrument_cache,
            ),
        )

    def _check_token(self) -> CheckResult:
        token = self.resolve_active_token()
        if not token:
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message="No Upstox token configured in secrets (analytics or oauth)",
                fix="Run 'algodesk login upstox' or configure secret:upstox_analytics_token",
            )
        session = AuthSession(token=token)
        if not self._auth.validate(session):
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message="Configured Upstox token failed validation",
                fix="Run 'algodesk login upstox' to re-authenticate",
            )
        exp = self._auth.expires_at(session)
        if exp is not None:
            now = datetime.now(exp.tzinfo)
            if (exp - now).total_seconds() < 0:
                return CheckResult(
                    ok=False,
                    severity=Severity.WARNING,
                    message=f"Upstox token expired on {exp.strftime('%Y-%m-%d')}",
                    fix="Run 'algodesk login upstox' to renew",
                )
        return CheckResult(ok=True, message="Upstox token is valid and active")

    def _check_redirect_port(self) -> CheckResult:
        import socket
        import urllib.parse

        parsed = urllib.parse.urlparse(self._cfg.redirect_uri)
        port = parsed.port or 5000
        host = parsed.hostname or "127.0.0.1"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
            return CheckResult(ok=True, message=f"Redirect port {port} is free on {host}")
        except OSError:
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message=f"Redirect port {port} is in use on {host}",
                fix=f"Free port {port} (disable AirPlay Receiver on macOS) or update redirect_uri",
            )

    def _check_instrument_cache(self) -> CheckResult:
        from datetime import UTC

        today = datetime.now(UTC).date()
        cache_file = self._instruments_mgr._cache_path(today)
        if not cache_file.is_file():
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message="Instrument cache is absent",
                fix="Instruments will be downloaded automatically on first run",
            )
        age_hours = (datetime.now(UTC).timestamp() - cache_file.stat().st_mtime) / 3600.0
        max_hours = float(self._instruments_mgr._param("upstox.instrument_cache_hours", 24))
        if age_hours <= max_hours:
            return CheckResult(
                ok=True,
                message=f"Instrument cache is fresh ({age_hours:.1f}h <= {max_hours:.0f}h)",
            )
        return CheckResult(
            ok=False,
            severity=Severity.WARNING,
            message=f"Instrument cache is stale ({age_hours:.1f}h > {max_hours:.0f}h)",
            fix="Refresh instrument master cache",
        )
