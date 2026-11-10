"""Upstox instrument master cache, download, and canonical ID mapping."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from algodesk.api import (
    AlgoDeskError,
    ConfigError,
    DataError,
    Instrument,
    InstrumentId,
    PluginContext,
    resolve_alias,
)

IST = ZoneInfo("Asia/Kolkata")


class UpstoxInstrumentManager:
    """Manages daily download, caching, and mapping of Upstox instruments."""

    def __init__(
        self,
        ctx: PluginContext | None = None,
        cache_dir: Path | None = None,
        *,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._ctx = ctx
        self._transport = http_transport
        if cache_dir is not None:
            self._cache_dir = cache_dir
        elif Path("local/cache/instruments").is_dir():
            self._cache_dir = Path("local/cache/instruments")
        else:
            self._cache_dir = Path(".local/cache/instruments")
        self._instruments: dict[str, Instrument] = {}
        self._key_to_inst: dict[str, Instrument] = {}
        self._symbol_to_inst: dict[str, Instrument] = {}

    def _cleanup_older_downloads(self, keep_path: Path) -> None:
        """Remove older cached instrument master files so only the new download is kept."""
        try:
            keep_resolved = keep_path.resolve()
        except Exception:
            return

        if not self._cache_dir.is_dir():
            return
        try:
            for file_path in self._cache_dir.iterdir():
                if (
                    file_path.is_file()
                    and file_path.name.startswith("upstox_master_")
                    and file_path.resolve() != keep_resolved
                ):
                    try:
                        file_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception:
            pass

    def _param(self, name: str, default: Any) -> Any:
        if self._ctx is not None and self._ctx.params is not None:
            try:
                val = self._ctx.params.get(name)
                return val if val is not None else default
            except Exception:
                return default
        return default

    def _cache_path(self, today: date) -> Path:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir / f"upstox_master_{today.isoformat()}.json.gz"

    def is_cache_fresh(self, today: date) -> bool:
        """Check whether instrument cache exists and is within configured max age."""
        p = self._cache_path(today)
        if not p.is_file():
            return False
        max_hours = float(self._param("upstox.instrument_cache_hours", 24))
        age_hours = (datetime.now(UTC).timestamp() - p.stat().st_mtime) / 3600.0
        return age_hours < max_hours

    def download_master(
        self,
        url: str = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz",
    ) -> bytes:
        """Download remote instrument master file with gzip compression."""
        timeout = float(self._param("upstox.instrument_download_timeout_s", 60.0))
        headers = {"Accept-Encoding": "gzip"}
        with httpx.Client(transport=self._transport, timeout=timeout) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                raise AlgoDeskError(
                    what=f"Instrument master download failed with HTTP {resp.status_code}",
                    where="UpstoxInstrumentManager.download_master",
                    fix=f"Verify Upstox asset URL: {url}",
                )
            return resp.content

    def load_or_fetch(
        self, raw_bytes: bytes | None = None, force_refresh: bool = False
    ) -> list[Instrument]:
        """Load instruments from local cache or download fresh file."""
        today = datetime.now(IST).date()
        p = self._cache_path(today)

        refresh_flag = (
            force_refresh
            or bool(self._param("upstox.refresh_master", False))
            or os.environ.get("UPSTOX_REFRESH_MASTER") in ("1", "true", "True")
        )

        if raw_bytes is not None:
            content = raw_bytes
            p.write_bytes(content)
            self._cleanup_older_downloads(p)
        elif not refresh_flag and self.is_cache_fresh(today):
            print("master cached")
            content = p.read_bytes()
            self._cleanup_older_downloads(p)
        else:
            import sys

            sys.stderr.write("Downloading Upstox instrument master (~3.4 MB)...\n")
            sys.stderr.flush()
            content = self.download_master()
            p.write_bytes(content)
            self._cleanup_older_downloads(p)

        sha = hashlib.sha256(content).hexdigest()
        if self._ctx is not None and self._ctx.journal is not None:
            try:
                self._ctx.journal.append(
                    "upstox.instruments.cached", {"sha256": sha, "path": str(p)}
                )
            except Exception:
                pass

        decompressed = gzip.decompress(content) if content.startswith(b"\x1f\x8b") else content
        return self.parse_and_index(decompressed)

    def parse_and_index(self, data: bytes | str) -> list[Instrument]:
        """Parse JSON or CSV payload and build lookup indices."""
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        entries: list[dict[str, Any]] = []

        trimmed = text.strip()
        if trimmed.startswith("[") or trimmed.startswith("{"):
            parsed = json.loads(trimmed)
            entries = parsed if isinstance(parsed, list) else parsed.get("data", [])
        else:
            reader = csv.DictReader(io.StringIO(trimmed))
            entries = list(reader)

        result: list[Instrument] = []
        self._instruments.clear()
        self._key_to_inst.clear()
        self._symbol_to_inst.clear()

        for row in entries:
            try:
                inst = self._map_row(row)
            except Exception:
                continue
            result.append(inst)
            self._instruments[str(inst.id)] = inst
            upstox_key = inst.broker_keys.get("upstox")
            if upstox_key:
                self._key_to_inst[upstox_key] = inst
            sym = row.get("tradingsymbol") or row.get("trading_symbol") or inst.symbol
            self._symbol_to_inst[str(sym).upper()] = inst
            if inst.trading_symbol:
                self._symbol_to_inst[inst.trading_symbol.upper()] = inst

        return result

    def _parse_upstox_expiry(self, val: Any) -> date | None:
        if val is None or val == "":
            return None
        if isinstance(val, (int, float)):
            return datetime.fromtimestamp(val / 1000.0, tz=IST).date()
        s = str(val).strip()
        if s.isdigit():
            return datetime.fromtimestamp(int(s) / 1000.0, tz=IST).date()
        if "-" in s:
            try:
                return date.fromisoformat(s)
            except Exception:
                pass
        return None

    def _map_row(self, row: dict[str, Any]) -> Instrument:
        key = str(row.get("instrument_key", "")).strip()
        if not key or "|" not in key:
            raise DataError(
                what=f"Cannot map Upstox record: missing or invalid instrument_key: {row!r}",
                where="UpstoxInstrumentManager._map_row",
                fix="Ensure instrument master record has valid instrument_key",
            )

        seg_prefix, identifier = key.split("|", 1)
        raw_exch = str(row.get("exchange") or seg_prefix.split("_")[0]).strip().upper()
        exchange = raw_exch if raw_exch else "NSE"
        seg_type = str(row.get("segment") or seg_prefix).strip().upper()
        itype = str(row.get("instrument_type") or "").strip().upper()
        trading_symbol = (
            str(row.get("tradingsymbol") or row.get("trading_symbol") or identifier).strip().upper()
        )

        if itype == "FUT":
            canonical_seg = "FUT"
        elif itype in ("CE", "PE"):
            canonical_seg = "OPT"
        elif itype == "INDEX" or "INDEX" in seg_type:
            canonical_seg = "INDEX"
        elif "EQ" in seg_type or itype in (
            "EQ",
            "EQUITY",
            "A",
            "B",
            "T",
            "BE",
            "SM",
            "BZ",
            "Z",
            "M",
            "MT",
            "ST",
            "E",
            "P",
            "X",
            "XT",
            "G",
            "GS",
            "TB",
            "GB",
            "F",
        ):
            canonical_seg = "EQ"
        elif "COM" in seg_type and itype == "COM":
            canonical_seg = "COM"
        elif seg_type == "GLOBAL_INDICATOR" or itype == "INDICATOR":
            canonical_seg = "INDICATOR"
        else:
            raise DataError(
                what=(
                    f"Cannot map Upstox record '{key}': unsupported segment "
                    f"'{seg_type}' and type '{itype}'"
                ),
                where="UpstoxInstrumentManager._map_row",
                fix="Check instrument master record or support this market segment",
            )

        expiry: date | None = None
        strike: Decimal | None = None
        option_type: str | None = None

        if canonical_seg in ("FUT", "OPT"):
            raw_exp = row.get("expiry") or row.get("last_trading_date")
            expiry = self._parse_upstox_expiry(raw_exp)
            if expiry is None:
                raise DataError(
                    what=(
                        f"Cannot map Upstox record '{key}': missing or invalid expiry date "
                        f"({raw_exp!r})"
                    ),
                    where="UpstoxInstrumentManager._map_row",
                    fix="Verify expiry field in master record",
                )
            und = str(row.get("underlying_symbol") or row.get("asset_symbol") or "").strip().upper()
            if not und:
                und = trading_symbol.split()[0]
            underlying = und
            symbol = underlying

            if canonical_seg == "OPT":
                raw_strike = row.get("strike_price") or row.get("strike")
                if raw_strike is None or raw_strike == "":
                    raise DataError(
                        what=f"Cannot map Upstox option record '{key}': missing strike price",
                        where="UpstoxInstrumentManager._map_row",
                        fix="Verify strike_price in master record",
                    )
                try:
                    k_dec = Decimal(str(raw_strike))
                    if k_dec == k_dec.to_integral_value():
                        strike = Decimal(int(k_dec))
                    else:
                        strike = k_dec.normalize()
                except Exception as e:
                    raise DataError(
                        what=(
                            f"Cannot map Upstox option record '{key}': invalid strike price "
                            f"{raw_strike!r}"
                        ),
                        where="UpstoxInstrumentManager._map_row",
                        fix="Verify strike_price in master record",
                    ) from e
                if itype not in ("CE", "PE"):
                    raise DataError(
                        what=(
                            f"Cannot map Upstox option record '{key}': invalid option type "
                            f"{itype!r}"
                        ),
                        where="UpstoxInstrumentManager._map_row",
                        fix="Option instrument_type must be CE or PE",
                    )
                option_type = itype
        else:
            symbol = trading_symbol
            underlying = symbol

        try:
            lot = int(row.get("lot_size") or row.get("minimum_lot") or 1)
        except (ValueError, TypeError):
            lot = 1
        if lot <= 0:
            lot = 1

        try:
            tick = Decimal(str(row.get("tick_size") or "0.05"))
        except Exception:
            tick = Decimal("0.05")
        if tick <= Decimal(0):
            tick = Decimal("0.05")

        isin = str(row.get("isin") or (identifier if identifier.startswith("IN") else "")).strip()
        broker_keys: dict[str, str] = {"upstox": key}
        if isin:
            broker_keys["isin"] = isin
        token = row.get("exchange_token")
        if token:
            broker_keys["exchange_token"] = str(token)

        return Instrument(
            exchange=exchange,
            segment=canonical_seg,
            symbol=symbol,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            lot_size=lot,
            tick_size=tick,
            isin=isin or None,
            trading_symbol=trading_symbol,
            broker_keys=broker_keys,
        )

    def resolve(self, symbol_or_id: str | InstrumentId) -> Instrument:
        """Resolve symbol, raw key, or canonical InstrumentId to Instrument."""
        fetch_error: Exception | None = None
        if not self._instruments:
            try:
                self.load_or_fetch()
            except Exception as e:
                fetch_error = e

        raw = str(symbol_or_id).strip()

        try:
            resolved_id = str(resolve_alias(raw))
            if resolved_id in self._instruments:
                return self._instruments[resolved_id]
        except Exception:
            pass

        if raw in self._instruments:
            return self._instruments[raw]
        if raw in self._key_to_inst:
            return self._key_to_inst[raw]
        upper_sym = (raw.split(":")[-1] if ":" in raw else raw).upper()
        if upper_sym in self._symbol_to_inst:
            return self._symbol_to_inst[upper_sym]

        if fetch_error is not None:
            raise ConfigError(
                what=(
                    f"Cannot map symbol '{symbol_or_id}': Upstox instrument master "
                    f"failed to load ({fetch_error})"
                ),
                where="UpstoxInstrumentManager.resolve",
                fix="Verify network connection to assets.upstox.com or check timeout settings",
            ) from fetch_error

        raise ConfigError(
            what=f"Cannot map unknown symbol '{symbol_or_id}' in Upstox instrument master",
            where="UpstoxInstrumentManager.resolve",
            fix="Check trading symbol spelling or ensure instrument master is loaded",
        )

    def resolve_isin(self, symbol_or_id: str | InstrumentId) -> str:
        """Resolve symbol or instrument id to ISIN for fundamentals API."""
        raw = str(symbol_or_id).strip()
        if raw.startswith("IN") and len(raw) == 12 and raw.isalnum():
            return raw
        inst = self.resolve(symbol_or_id)
        if "isin" in inst.broker_keys and inst.broker_keys["isin"]:
            return inst.broker_keys["isin"]
        upstox_key = inst.broker_keys.get("upstox", "")
        if "|" in upstox_key:
            ident = upstox_key.split("|", 1)[1]
            if ident.startswith("IN") and len(ident) == 12:
                return ident
        return inst.symbol

    def all_instruments(self) -> Iterable[Instrument]:
        """Return all indexed instruments."""
        return self._instruments.values()
