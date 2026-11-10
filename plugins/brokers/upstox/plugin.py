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
    DataError,
    DoctorCheck,
    FeedAuthError,
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


class AuthRequired(AuthError):
    """Raised when an operation requires an OAuth access token."""


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
    name_raw = str(
        ev.get("name") or ev.get("type") or ev.get("event_type") or ev.get("purpose") or ""
    ).lower()
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
    reason = ""
    if ev_type in ("split", "bonus") and ratio_val is None:
        status = "unverified"
        reason = "Missing ratio in Upstox response"
    elif ev_type == "other":
        status = "manual_required"
        reason = "Unrecognized corporate action type"
    else:
        status = "unverified"

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
        reason=reason,
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
        _data_dir: Path | None = None,
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
        self._data_dir = _data_dir

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

    def resolve_oauth_token(self) -> str | None:
        """Resolve active OAuth token from session or secrets."""
        if self._session is not None and self._session.token:
            return self._session.token
        reader = self.ctx.secrets if self.ctx is not None else None
        try:
            tok = resolve_secret_ref(self._cfg.access_token, reader)
            if tok:
                return tok
        except Exception:
            pass
        return None

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

    def resolve_active_token_type(self) -> str:
        """Return the active token type: 'oauth', 'analytics', or 'none'."""
        if self._session is not None and self._session.token:
            return "oauth"
        reader = self.ctx.secrets if self.ctx is not None else None
        for kind in self._cfg.auth_priority:
            if kind == "oauth":
                try:
                    if resolve_secret_ref(self._cfg.access_token, reader):
                        return "oauth"
                except Exception:
                    pass
            elif kind == "analytics":
                try:
                    if resolve_secret_ref(self._cfg.analytics_token, reader):
                        return "analytics"
                except Exception:
                    pass
        return "none"

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
        """Fetch corporate actions data from Upstox using OAuth access token."""
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

        token = self.resolve_oauth_token()
        if not token:
            raise AuthRequired(
                what="Upstox corporate actions requires an active OAuth access token",
                where="UpstoxBrokerPlugin.actions",
                fix="run algodesk login upstox",
            )
        if self._session is not None and self._session.expires_at is not None:
            now = datetime.now(self._session.expires_at.tzinfo or UTC)
            if (self._session.expires_at - now).total_seconds() <= 0:
                raise AuthRequired(
                    what="Upstox OAuth access token has expired",
                    where="UpstoxBrokerPlugin.actions",
                    fix="run algodesk login upstox",
                )

        url = f"https://api.upstox.com/v2/fundamentals/{isin}/corporate-actions"
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

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
            raise AuthRequired(
                what="Upstox authentication failed on corporate actions",
                where="UpstoxBrokerPlugin.actions",
                fix="run algodesk login upstox",
            )
        if resp.status_code != 200:
            raise DataError(
                what=f"Upstox corporate actions error: HTTP {resp.status_code}",
                where="UpstoxBrokerPlugin.actions",
                fix="Verify symbol or ISIN identifier",
            )

        try:
            payload = resp.json()
        except Exception as exc:
            raise DataError(
                what=f"Invalid JSON in Upstox corporate actions response for {sym_str}",
                where="UpstoxBrokerPlugin.actions",
                fix="Check Upstox API response",
            ) from exc

        if not isinstance(payload, dict):
            p_type = type(payload).__name__
            raise DataError(
                what=f"Unexpected Upstox corporate actions shape: expected dict, got {p_type}",
                where="UpstoxBrokerPlugin.actions",
                fix="Check Upstox corporate actions response format",
            )

        events_data = payload.get("data")
        if events_data is None:
            events_data = payload.get("events")
        if events_data is None or not isinstance(events_data, list):
            raise DataError(
                what="Unexpected Upstox corporate actions payload shape: missing events list",
                where="UpstoxBrokerPlugin.actions",
                fix="Verify Upstox corporate actions response structure",
            )

        now_iso = datetime.now(UTC).isoformat()
        return [
            _parse_upstox_action(ev, clean_sym, exchange, isin, now_iso)
            for ev in events_data
            if isinstance(ev, dict)
        ]

    def probe_actions_raw(self, symbol: str | InstrumentId) -> dict[str, Any]:
        """Diagnostic probe returning raw response metadata without disclosing tokens."""
        self.require("corporate_actions")
        sym_str = str(symbol).strip()
        try:
            isin = self._instruments_mgr.resolve_isin(sym_str)
        except Exception:
            isin = sym_str

        url = f"https://api.upstox.com/v2/fundamentals/{isin}/corporate-actions"
        token = self.resolve_oauth_token()
        token_type = "oauth" if token else "none"
        if not token:
            token = self.resolve_active_token()
            if token:
                token_type = "analytics"

        sec_names: list[str] = []
        reader = self.ctx.secrets if self.ctx is not None else None
        if reader is not None and hasattr(reader, "names"):
            try:
                sec_names = [n for n in reader.names() if "upstox" in n.lower()]
            except Exception:
                pass
        if reader is not None:
            for k in [
                "upstox_access_token",
                "upstox_analytics_token",
                "upstox_client_id",
                "upstox_client_secret",
                "upstox_api_key",
                "upstox_api_secret",
            ]:
                if k not in sec_names:
                    try:
                        if reader.get(k):
                            sec_names.append(k)
                    except Exception:
                        pass
        sec_names = sorted(set(sec_names))

        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        client = self._client()
        try:
            resp = client.get(url, headers=headers)
            status_code = resp.status_code
            try:
                payload = resp.json()
            except Exception:
                # pyrefly: ignore[implicit-any-empty-container]
                payload = {}
        except Exception as exc:
            return {
                "symbol": sym_str,
                "isin": isin,
                "token_type": token_type,
                "secrets_present": sec_names,
                "status_code": 0,
                "top_level_keys": [],
                "first_item_keys": [],
                "error": str(exc),
            }

        top_keys = list(payload.keys()) if isinstance(payload, dict) else []
        items = payload.get("data") if isinstance(payload, dict) else None
        if items is None and isinstance(payload, dict):
            items = payload.get("events")
        item_list = items if isinstance(items, list) else []
        first_item = item_list[0] if item_list and isinstance(item_list[0], dict) else {}
        first_item_keys = list(first_item.keys()) if isinstance(first_item, dict) else []

        if status_code != 200:
            msg = payload.get("message") or f"HTTP {status_code}"
            return {
                "symbol": sym_str,
                "isin": isin,
                "token_type": token_type,
                "secrets_present": sec_names,
                "status_code": status_code,
                "top_level_keys": top_keys,
                "first_item_keys": first_item_keys,
                "error": str(msg),
            }

        return {
            "symbol": sym_str,
            "isin": isin,
            "token_type": token_type,
            "secrets_present": sec_names,
            "status_code": status_code,
            "top_level_keys": top_keys,
            "first_item_keys": first_item_keys,
            "item_count": len(item_list),
            "first_item": first_item,
        }

    async def stream(self, instruments: Sequence[InstrumentId]) -> AsyncIterator[MarketEvent]:
        """Stream real-time MarketEvents over WebSocket feed."""
        self.require("stream")
        token = self.resolve_active_token()
        if not token:
            raise FeedAuthError(
                what="No valid Upstox authentication token found for streaming",
                where="UpstoxBrokerPlugin.stream",
                fix="run algodesk login upstox",
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
            DoctorCheck(
                id="upstox.actions_oauth",
                severity=Severity.WARNING,
                run=self._check_actions_oauth,
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
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
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

    def _check_actions_oauth(self) -> CheckResult:
        token = self.resolve_oauth_token()
        if token and self._auth.validate(AuthSession(token=token)):
            return CheckResult(
                ok=True, message="Upstox OAuth token is active for corporate actions"
            )

        ttl = int(self._param("data.actions_ttl_hours", 24))
        actions_dir = (self._data_dir or Path("data")) / "actions"
        is_stale = False
        if actions_dir.exists():
            now_ts = datetime.now(UTC).timestamp()
            for f in actions_dir.glob("exchange=*/symbol=*/actions.parquet"):
                age_h = (now_ts - f.stat().st_mtime) / 3600.0
                if age_h > ttl:
                    is_stale = True
                    break

        if is_stale:
            msg = (
                f"Corporate actions are older than data.actions_ttl_hours ({ttl}h) "
                "and OAuth is missing"
            )
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message=msg,
                fix="run algodesk login upstox",
            )
        return CheckResult(ok=True, message="Corporate actions are within TTL or OAuth not needed")
