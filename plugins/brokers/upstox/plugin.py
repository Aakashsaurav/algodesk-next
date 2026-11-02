"""Upstox broker plugin implementing DataSource and AuthFlow protocols."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx

from algodesk.api import (
    ActionRecord,
    AlgoDeskError,
    AuthError,
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
    RateLimitError,
    SeriesKey,
    Severity,
    resolve_secret_ref,
)

from .auth import UpstoxAuthFlow
from .config_schema import UpstoxConfig
from .feed import UpstoxWebSocketFeed
from .history import UpstoxHistoryClient
from .instruments import UpstoxInstrumentManager


def _parse_upstox_action_date(val: Any) -> date | None:
    if not val:
        return None
    s = str(val).strip()
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        pass
    for fmt in ("%d %b %Y", "%d-%b-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def _parse_upstox_action(
    ev: dict[str, Any],
    symbol: str,
    exchange: str,
    isin: str,
    now_iso: str,
) -> ActionRecord:
    name_raw = str(ev.get("name") or ev.get("type") or ev.get("event_type") or "").lower()
    if "split" in name_raw:
        ev_type = "split"
    elif "bonus" in name_raw:
        ev_type = "bonus"
    elif "dividend" in name_raw:
        ev_type = "dividend"
    elif "rights" in name_raw:
        ev_type = "rights"
    elif "demerger" in name_raw:
        ev_type = "demerger"
    elif "merger" in name_raw:
        ev_type = "merger"
    elif "buyback" in name_raw:
        ev_type = "buyback"
    else:
        ev_type = "other"

    status = "manual_required" if ev_type == "other" else "unverified"

    ex_d = _parse_upstox_action_date(ev.get("ex_date") or ev.get("expiry_date"))
    if not ex_d:
        details = ev.get("event_details") or []
        if isinstance(details, list):
            for item in details:
                if isinstance(item, dict) and "ex" in str(item.get("name", "")).lower():
                    ex_d = _parse_upstox_action_date(item.get("value"))
                    if ex_d:
                        break
    if not ex_d:
        ex_d = datetime.now(UTC).date()

    ratio_val: float | None = None
    ratio_raw = ev.get("ratio")
    if ratio_raw is not None:
        try:
            if isinstance(ratio_raw, (int, float)):
                ratio_val = float(ratio_raw)
            elif ":" in str(ratio_raw):
                p = str(ratio_raw).split(":")
                ratio_val = float(p[0]) / float(p[1])
            elif "/" in str(ratio_raw):
                p = str(ratio_raw).split("/")
                ratio_val = float(p[0]) / float(p[1])
            else:
                ratio_val = float(ratio_raw)
        except Exception:
            ratio_val = None

    amt_val: Decimal | None = None
    amt_raw = ev.get("amount")
    if amt_raw is not None:
        try:
            amt_val = Decimal(str(amt_raw))
        except Exception:
            amt_val = None

    text = str(ev.get("details") or ev.get("description") or name_raw)
    return ActionRecord(
        ex_date=ex_d,
        type=ev_type,
        ratio=ratio_val,
        amount=amt_val,
        text=text,
        source="upstox",
        source_ref=isin,
        fetched_at=now_iso,
        symbol=symbol,
        exchange=exchange,
        status=status,
    )


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
        self._http_client: httpx.Client | None = None

    def _param(self, name: str, default: Any) -> Any:
        if hasattr(self, "ctx") and self.ctx is not None and self.ctx.params is not None:
            try:
                val = self.ctx.params.get(name)
                return val if val is not None else default
            except Exception:
                return default
        return default

    def _client(self) -> httpx.Client:
        """Reuse single persistent HTTP client across requests."""
        if self._http_client is None or self._http_client.is_closed:
            timeout_s = float(self._param("upstox.request_timeout_s", 15.0))
            self._http_client = httpx.Client(
                transport=self._http_transport,
                timeout=timeout_s,
            )
        return self._http_client

    def close(self) -> None:
        """Close persistent HTTP client if open."""
        if self._http_client is not None and not self._http_client.is_closed:
            self._http_client.close()

    @property
    def auth(self) -> UpstoxAuthFlow:
        """Access the underlying AuthFlow handler."""
        return self._auth

    def begin(self) -> LoginRequest:
        return self._auth.begin()

    def begin_auto(
        self,
        *,
        timeout_s: float | None = None,
        open_browser: bool = True,
    ) -> AuthSession:
        self._session = self._auth.begin_auto(timeout_s=timeout_s, open_browser=open_browser)
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

    def actions(self, symbol: str | InstrumentId) -> Sequence[ActionRecord]:
        """Fetch corporate actions data from Upstox fundamentals endpoint."""
        self.require("corporate_actions")
        sym_str = str(symbol).strip()
        try:
            inst = self._instruments_mgr.resolve(sym_str)
            isin = self._instruments_mgr.resolve_isin(sym_str)
            clean_sym = inst.symbol
            exchange = inst.exchange
        except Exception:
            isin = sym_str
            clean_sym = sym_str.split(":")[-1]
            exchange = "NSE"

        url = f"https://api.upstox.com/v2/fundamentals/{isin}/corporate-actions"
        token = self.resolve_active_token()
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        client = self._client()
        try:
            resp = client.get(url, headers=headers)
        except Exception as exc:
            raise AlgoDeskError(
                what=f"Failed to query Upstox corporate actions for {sym_str}: {exc}",
                where="UpstoxBrokerPlugin.actions",
                fix="Check network connectivity or credentials",
            ) from exc

        if resp.status_code == 429:
            raise RateLimitError(
                what="Upstox rate limit exceeded on corporate actions",
                where="UpstoxBrokerPlugin.actions",
                fix="Reduce request rate or wait before retrying",
            )
        if resp.status_code in (401, 403):
            raise AuthError(
                what="Upstox authentication failed on corporate actions",
                where="UpstoxBrokerPlugin.actions",
                fix="Run 'algodesk login upstox' or provide active analytics token",
            )
        if resp.status_code in (400, 404):
            return []

        if resp.status_code != 200:
            raise AlgoDeskError(
                what=f"Upstox corporate actions error: HTTP {resp.status_code}",
                where="UpstoxBrokerPlugin.actions",
                fix="Verify symbol or ISIN identifier",
            )

        payload = resp.json()
        events_data = payload.get("data")
        if events_data is None:
            events_data = payload.get("events", [])
        if not isinstance(events_data, list):
            return []

        now_iso = datetime.now(UTC).isoformat()
        return [
            _parse_upstox_action(ev, clean_sym, exchange, isin, now_iso)
            for ev in events_data
            if isinstance(ev, dict)
        ]

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
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
