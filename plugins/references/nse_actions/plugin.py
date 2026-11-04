"""NSE corporate actions reference plugin implementation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from algodesk.api.errors import DataError
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import ActionRecord
from algodesk.api.plugin import PluginBase
from algodesk.api.services import PluginContext

from .parser import parse_nse_item

_NSE_ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
}


class NSEActionsReferencePlugin(PluginBase):
    """ReferenceSource plugin providing verified corporate actions from NSE."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: Mapping[str, Any] | None = None,
        ctx: PluginContext | None = None,
        *,
        _transport: httpx.BaseTransport | None = None,
        _fixture_path: Path | str | None = None,
        _data_dir: Path | str | None = None,
    ) -> None:
        self.manifest = manifest
        self._cfg = dict(config or {})
        self._ctx = ctx
        self._transport = _transport
        self._fixture_path = Path(_fixture_path) if _fixture_path else None
        self._data_dir = Path(_data_dir) if _data_dir else Path("data")

    def _cache_raw_response(self, symbol: str, raw_bytes: bytes) -> Path:
        raw_dir = self._data_dir / "actions" / "raw" / "nse"
        raw_dir.mkdir(parents=True, exist_ok=True)
        today_str = datetime.now(UTC).date().strftime("%Y%m%d")
        target = raw_dir / f"{symbol.upper()}_{today_str}.json"
        target.write_bytes(raw_bytes)
        return target

    def _fetch_from_api(
        self,
        symbol: str,
        start: date | None,
        end: date | None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"index": "equities", "symbol": symbol}
        if start:
            params["from_date"] = start.strftime("%d-%m-%Y")
        if end:
            params["to_date"] = end.strftime("%d-%m-%Y")

        client_kwargs: dict[str, Any] = {"headers": _HEADERS, "timeout": 15.0}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.get(_NSE_ACTIONS_URL, params=params)
                if resp.status_code != 200:
                    raise DataError(
                        what=f"NSE API returned HTTP {resp.status_code} for {symbol}",
                        where="NSEActionsReferencePlugin.corporate_actions",
                        fix="Check NSE availability or RateLimiter backoff",
                    )
                self._cache_raw_response(symbol, resp.content)
                data = resp.json()
                return data if isinstance(data, list) else []
        except DataError:
            raise
        except Exception as exc:
            raise DataError(
                what=f"Failed to fetch corporate actions from NSE for {symbol}: {exc}",
                where="NSEActionsReferencePlugin.corporate_actions",
                fix="Ensure internet connectivity and valid User-Agent",
            ) from exc

    def corporate_actions(
        self,
        symbol_or_isin: str,
        start: date | None = None,
        end: date | None = None,
    ) -> Sequence[ActionRecord]:
        """Fetch and parse corporate actions for an instrument symbol or ISIN."""
        clean_sym = symbol_or_isin.split(":")[-1].strip().upper()
        if self._fixture_path is not None and self._fixture_path.is_file():
            content = self._fixture_path.read_bytes()
            self._cache_raw_response(clean_sym, content)
            items = json.loads(content.decode("utf-8"))
        else:
            items = self._fetch_from_api(clean_sym, start, end)

        now_iso = datetime.now(UTC).isoformat()
        records: list[ActionRecord] = []
        for item in items:
            if isinstance(item, dict):
                parsed = parse_nse_item(item, now_iso=now_iso)
                for act in parsed:
                    if start and act.ex_date < start:
                        continue
                    if end and act.ex_date > end:
                        continue
                    records.append(act)

        return sorted(records, key=lambda a: (a.ex_date, a.type))
