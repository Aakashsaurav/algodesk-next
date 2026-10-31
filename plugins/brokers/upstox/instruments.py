"""Upstox instrument master cache, download, and canonical ID mapping."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from algodesk.api import (
    AlgoDeskError,
    ConfigError,
    Instrument,
    InstrumentId,
    PluginContext,
)


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
        dirs_to_clean: list[Path] = [self._cache_dir]
        for alt in (Path(".local/cache/instruments"), Path("local/cache/instruments")):
            try:
                if alt.is_dir() and alt.resolve() not in [d.resolve() for d in dirs_to_clean]:
                    dirs_to_clean.append(alt)
            except Exception:
                pass

        try:
            keep_resolved = keep_path.resolve()
        except Exception:
            return

        for d in dirs_to_clean:
            if not d.is_dir():
                continue
            try:
                for file_path in d.iterdir():
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

    def load_or_fetch(self, raw_bytes: bytes | None = None) -> list[Instrument]:
        """Load instruments from local cache or download fresh file."""
        today = datetime.now(UTC).date()
        p = self._cache_path(today)

        if raw_bytes is not None:
            content = raw_bytes
            p.write_bytes(content)
            self._cleanup_older_downloads(p)
        elif self.is_cache_fresh(today):
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
            inst = self._map_row(row)
            if inst is not None:
                result.append(inst)
                self._instruments[str(inst.id)] = inst
                upstox_key = inst.broker_keys.get("upstox")
                if upstox_key:
                    self._key_to_inst[upstox_key] = inst
                sym = row.get("tradingsymbol") or row.get("trading_symbol") or inst.symbol
                self._symbol_to_inst[sym.upper()] = inst

        return result

    def _map_row(self, row: dict[str, Any]) -> Instrument | None:
        key = row.get("instrument_key", "")
        if not key or "|" not in key:
            return None

        seg_prefix, identifier = key.split("|", 1)
        parts = seg_prefix.split("_")
        exchange = parts[0].upper()
        seg_type = parts[1].upper() if len(parts) > 1 else "EQ"
        raw_symbol = row.get("tradingsymbol") or row.get("trading_symbol") or identifier
        symbol = str(raw_symbol).upper()

        canonical_seg = "EQ" if seg_type in ("EQ", "INDEX") else "FNO"
        canonical_id = InstrumentId(f"{exchange}:{canonical_seg}:{symbol}")

        try:
            lot = int(row.get("lot_size") or 1)
        except (ValueError, TypeError):
            lot = 1

        try:
            tick = Decimal(str(row.get("tick_size") or "0.05"))
        except Exception:
            tick = Decimal("0.05")

        return Instrument(
            id=canonical_id,
            exchange=exchange,
            segment=canonical_seg,
            symbol=symbol,
            kind="equity" if canonical_seg == "EQ" else "fno",
            lot_size=lot,
            tick_size=tick,
            broker_keys={"upstox": key},
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

    def all_instruments(self) -> Iterable[Instrument]:
        """Return all indexed instruments."""
        return self._instruments.values()
