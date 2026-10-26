"""Upstox authentication flow supporting analytics tokens and OAuth2."""

from __future__ import annotations

import secrets
import urllib.parse
import webbrowser
from datetime import UTC, date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from algodesk.api import (
    AlgoDeskError,
    AuthSession,
    ConfigError,
    LoginRequest,
    PluginContext,
    resolve_secret_ref,
)

from .config_schema import UpstoxConfig


class _OneShotServer(HTTPServer):
    """Custom HTTPServer holding callback state per server instance."""

    def __init__(
        self, server_address: tuple[str, int], RequestHandlerClass: type[BaseHTTPRequestHandler]
    ) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.received_code: str | None = None
        self.received_state: str | None = None
        self.error_message: str | None = None


class _OneShotHandler(BaseHTTPRequestHandler):
    """One-shot HTTP request handler for OAuth redirect loopback callback."""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        srv: Any = self.server
        srv.received_state = params.get("state", [None])[0]
        srv.received_code = params.get("code", [None])[0]

        if not srv.received_code:
            srv.error_message = params.get("error_description", ["Authorization failed"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Login Failed</h1><p>You can close this tab.</p></body></html>"
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>Authorization Successful</h1>"
            b"<p>You can close this tab.</p></body></html>"
        )

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress standard logging to prevent leaking codes or query strings
        _ = (format, args)


class UpstoxAuthFlow:
    """Implements AuthFlow protocol for Upstox."""

    def __init__(
        self,
        config: UpstoxConfig,
        ctx: PluginContext | None = None,
        *,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._cfg = config
        self._ctx = ctx
        self._transport = http_transport
        self._pending_state: str | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            timeout=float(self._param("upstox.request_timeout_s", 15.0)),
        )

    def _param(self, name: str, default: Any) -> Any:
        if self._ctx is not None and self._ctx.params is not None:
            try:
                val = self._ctx.params.get(name)
                return val if val is not None else default
            except Exception:
                return default
        return default

    def _get_client_creds(self) -> tuple[str, str]:
        reader = self._ctx.secrets if self._ctx is not None else None
        try:
            client_id = resolve_secret_ref(self._cfg.api_key, reader)
            client_secret = resolve_secret_ref(self._cfg.api_secret, reader)
            return client_id, client_secret
        except Exception as exc:
            msg = "Ensure upstox_client_id and upstox_client_secret are set in secrets"
            raise ConfigError(
                what="Failed to resolve Upstox API credentials",
                where="UpstoxAuthFlow",
                fix=msg,
            ) from exc

    def begin(self) -> LoginRequest:
        """Generate manual login URL and pending state."""
        client_id, _ = self._get_client_creds()
        self._pending_state = secrets.token_hex(16)
        query = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": self._cfg.redirect_uri,
                "response_type": "code",
                "state": self._pending_state,
            }
        )
        url = f"https://api.upstox.com/v2/login/authorization/dialog?{query}"
        instructions = "Open URL in browser, authorize app, and paste returned 'code' parameter."
        return LoginRequest(
            url=url,
            state=self._pending_state,
            instructions=instructions,
        )

    def complete(self, code: str) -> AuthSession:
        """Exchange authorization code for OAuth access token."""
        client_id, client_secret = self._get_client_creds()
        token_url = "https://api.upstox.com/v2/login/authorization/token"
        headers = {
            "Accept": "application/json",
            "Api-Version": "2.0",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": self._cfg.redirect_uri,
            "grant_type": "authorization_code",
        }

        with self._client() as client:
            resp = client.post(token_url, headers=headers, data=data)
            if resp.status_code != 200:
                raise AlgoDeskError(
                    what=f"Token exchange failed with HTTP {resp.status_code}",
                    where="UpstoxAuthFlow.complete",
                    fix="Verify authorization code and client secret credentials",
                )
            payload = resp.json()

        access_token = payload.get("access_token", "")
        user_id = payload.get("user_id", "")
        if not access_token:
            raise AlgoDeskError(
                what="Missing 'access_token' in token exchange response",
                where="UpstoxAuthFlow.complete",
                fix="Inspect Upstox API response envelope",
            )

        if self._ctx is not None and self._ctx.secrets is not None:
            secrets_svc: Any = self._ctx.secrets
            if hasattr(secrets_svc, "set") and callable(secrets_svc.set):
                try:
                    secrets_svc.set("upstox_access_token", access_token)
                except Exception:
                    pass

        # Upstox OAuth tokens expire at 03:30 AM IST of the following morning
        ist = ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(ist)
        tomorrow_ist = now_ist.date() + timedelta(days=1)
        exp_dt = datetime.combine(tomorrow_ist, time(3, 30), tzinfo=ist).astimezone(UTC)

        return AuthSession(
            token=access_token,
            expires_at=exp_dt,
            user_id=user_id,
            extra={"auth_type": "oauth"},
        )

    def begin_auto(self, *, timeout_s: float | None = None) -> AuthSession:
        """Run loopback server, open browser, and automatically capture redirect."""
        timeout = float(timeout_s or self._param("upstox.auth_timeout_seconds", 120))
        parsed = urllib.parse.urlparse(self._cfg.redirect_uri)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 5000

        login_req = self.begin()
        expected_state = login_req.state

        try:
            server = _OneShotServer((host, port), _OneShotHandler)
        except OSError as exc:
            msg = (
                "Ensure port 5000 is free (or disable AirPlay) or "
                "use 'algodesk login upstox --manual'"
            )
            raise AlgoDeskError(
                what=f"Cannot bind loopback server on {host}:{port} ({exc})",
                where="UpstoxAuthFlow.begin_auto",
                fix=msg,
            ) from exc

        server.timeout = timeout
        try:
            webbrowser.open(login_req.url)
        except Exception:
            pass

        try:
            server.handle_request()  # Handles exactly one request
        finally:
            server.server_close()

        if server.error_message:
            raise AlgoDeskError(
                what=f"OAuth authorization error: {server.error_message}",
                where="UpstoxAuthFlow.begin_auto",
                fix="Re-try login with valid Upstox credentials",
            )

        if not server.received_code:
            raise AlgoDeskError(
                what=f"Timed out after {timeout:.0f}s waiting for browser redirect",
                where="UpstoxAuthFlow.begin_auto",
                fix="Run 'algodesk login upstox --manual' to log in manually",
            )

        if server.received_state != expected_state:
            raise AlgoDeskError(
                what="OAuth state parameter mismatch (possible CSRF attempt)",
                where="UpstoxAuthFlow.begin_auto",
                fix="Initiate a fresh login ceremony",
            )

        return self.complete(server.received_code)

    def use_token(self, token: str, expires: date | None = None) -> AuthSession:
        """Create session using long-lived analytics token."""
        exp_dt: datetime | None = None
        if expires is not None:
            ist = ZoneInfo("Asia/Kolkata")
            exp_dt = datetime.combine(expires, time(23, 59, 59), tzinfo=ist).astimezone(UTC)

        return AuthSession(
            token=token,
            expires_at=exp_dt,
            extra={"auth_type": "analytics"},
        )

    def validate(self, session: AuthSession) -> bool:
        """Validate token by invoking lightweight authorized feed authorize endpoint."""
        url = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
        headers = {"Authorization": f"Bearer {session.token}", "Accept": "application/json"}
        try:
            with self._client() as client:
                resp = client.get(url, headers=headers)
                return resp.status_code == 200
        except Exception:
            return False

    def expires_at(self, session: AuthSession) -> datetime | None:
        """Return token expiration datetime if available."""
        return session.expires_at
