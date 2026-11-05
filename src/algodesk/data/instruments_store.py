"""Daily instrument master versioned storage, checksumming, and diffing.

Stores instrument masters under:
  data/instruments/source=S/date=YYYY-MM-DD/master.json.gz
  data/instruments/source=S/date=YYYY-MM-DD/checksum.sha256
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from algodesk.api.errors import DataError
from algodesk.api.market import Instrument, InstrumentId


@dataclass(frozen=True, slots=True)
class InstrumentChange:
    id: InstrumentId
    changes: dict[str, tuple[Any, Any]]  # field_name -> (old_val, new_val)


@dataclass(frozen=True, slots=True)
class InstrumentDiffReport:
    source: str
    date_a: date
    date_b: date
    added: list[Instrument] = field(default_factory=list)
    removed: list[Instrument] = field(default_factory=list)
    changed: list[InstrumentChange] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    def summary(self) -> str:
        return (
            f"Diff {self.source} ({self.date_a} -> {self.date_b}): "
            f"+{len(self.added)} added, -{len(self.removed)} removed, ~{len(self.changed)} changed"
        )


class InstrumentStore:
    """Manages versioned instrument master snapshots and daily diffs."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def _dir_path(self, source: str, dt: date) -> Path:
        return self._data_dir / "instruments" / f"source={source}" / f"date={dt.isoformat()}"

    def save_master(
        self,
        source: str,
        dt: date,
        raw_bytes: bytes,
        instruments: list[Instrument] | None = None,
    ) -> Path:
        """Store compressed daily master and compute sha256 checksum."""
        pdir = self._dir_path(source, dt)
        pdir.mkdir(parents=True, exist_ok=True)

        # Ensure gzipped
        gz_bytes = raw_bytes if raw_bytes.startswith(b"\x1f\x8b") else gzip.compress(raw_bytes)
        master_file = pdir / "master.json.gz"
        master_file.write_bytes(gz_bytes)

        sha = hashlib.sha256(gz_bytes).hexdigest()
        (pdir / "checksum.sha256").write_text(f"{sha}  master.json.gz\n", encoding="utf-8")

        return pdir

    def list_dates(self, source: str = "upstox") -> list[date]:
        """List all snapshot dates stored for a source."""
        source_dir = self._data_dir / "instruments" / f"source={source}"
        if not source_dir.is_dir():
            return []
        dates: list[date] = []
        for p in source_dir.glob("date=*"):
            if p.is_dir():
                d_str = p.name.split("=", 1)[1]
                try:
                    dates.append(date.fromisoformat(d_str))
                except ValueError:
                    pass
        return sorted(dates)

    def load_master(self, source: str, dt: date, parser: Any | None = None) -> list[Instrument]:
        """Load instruments for a given date and source."""
        pdir = self._dir_path(source, dt)
        master_file = pdir / "master.json.gz"
        if not master_file.is_file():
            # Check alternative filename
            master_file = pdir / "instruments.json.gz"
            if not master_file.is_file():
                raise DataError(
                    what=f"No instrument master snapshot found for {source} on {dt.isoformat()}",
                    where="InstrumentStore.load_master",
                    fix=f"Ensure daily master was recorded at {pdir}",
                )

        content = master_file.read_bytes()
        chk_file = pdir / "checksum.sha256"
        if chk_file.is_file():
            expected = chk_file.read_text(encoding="utf-8").strip().split()[0]
            actual = hashlib.sha256(content).hexdigest()
            if actual != expected:
                raise DataError(
                    what=(
                        f"Checksum mismatch for master snapshot at {master_file} "
                        f"({actual} vs {expected})"
                    ),
                    where="InstrumentStore.load_master",
                    fix="Re-fetch or restore corrupted snapshot",
                )

        decompressed = gzip.decompress(content) if content.startswith(b"\x1f\x8b") else content
        if parser is not None:
            return cast(list[Instrument], parser(decompressed))

        parsed = json.loads(decompressed.decode("utf-8"))
        res: list[Instrument] = []
        for item in parsed:
            # Map item
            inst = self._map_stored_record(item)
            if inst is not None:
                res.append(inst)
        return res

    def _map_stored_record(self, item: dict[str, Any]) -> Instrument | None:
        try:
            exch = str(item.get("exchange") or "NSE").upper()
            seg_raw = str(item.get("segment") or "EQ").upper()
            itype = str(item.get("instrument_type") or "").upper()
            raw_tsym = (
                item.get("tradingsymbol") or item.get("trading_symbol") or item.get("symbol") or ""
            )
            tsym = str(raw_tsym).upper()

            if itype == "FUT":
                seg = "FUT"
            elif itype in ("CE", "PE"):
                seg = "OPT"
            elif itype == "INDEX" or "INDEX" in seg_raw:
                seg = "INDEX"
            elif "EQ" in seg_raw or itype in ("EQ", "EQUITY"):
                seg = "EQ"
            else:
                seg = "EQ"

            expiry: date | None = None
            raw_exp = item.get("expiry") or item.get("last_trading_date")
            if raw_exp:
                if isinstance(raw_exp, (int, float)):
                    from datetime import datetime
                    from zoneinfo import ZoneInfo

                    expiry = datetime.fromtimestamp(
                        raw_exp / 1000.0, tz=ZoneInfo("Asia/Kolkata")
                    ).date()
                elif isinstance(raw_exp, str) and "-" in raw_exp:
                    expiry = date.fromisoformat(raw_exp)

            raw_und = (
                item.get("underlying_symbol")
                or item.get("asset_symbol")
                or item.get("underlying")
                or ""
            )
            und = str(raw_und).upper()
            if not und:
                und = tsym.split()[0] if tsym else "UNKNOWN"

            sym = und if seg in ("FUT", "OPT") else tsym
            strike_val: Decimal | None = None
            opt_type: str | None = None
            if seg == "OPT":
                raw_k = item.get("strike_price") or item.get("strike")
                if raw_k is not None:
                    k_dec = Decimal(str(raw_k))
                    if k_dec == k_dec.to_integral_value():
                        strike_val = Decimal(int(k_dec))
                    else:
                        strike_val = k_dec.normalize()
                opt_type = itype if itype in ("CE", "PE") else None

            lot = int(item.get("lot_size") or item.get("minimum_lot") or 1)
            tick = Decimal(str(item.get("tick_size") or "0.05"))
            isin = str(item.get("isin") or "")

            return Instrument(
                exchange=exch,
                segment=seg,
                symbol=sym,
                underlying=und,
                expiry=expiry,
                strike=strike_val,
                option_type=opt_type,
                lot_size=lot if lot > 0 else 1,
                tick_size=tick if tick > Decimal(0) else Decimal("0.05"),
                isin=isin or None,
                trading_symbol=tsym,
            )
        except Exception:
            return None

    def diff(
        self,
        source: str,
        date_a: date,
        date_b: date,
        journal: Any | None = None,
    ) -> InstrumentDiffReport:
        """Compute diff between two snapshot dates: added, removed, and changed attributes."""
        insts_a = {str(i.id): i for i in self.load_master(source, date_a)}
        insts_b = {str(i.id): i for i in self.load_master(source, date_b)}

        added: list[Instrument] = [insts_b[k] for k in sorted(insts_b.keys() - insts_a.keys())]
        removed: list[Instrument] = [insts_a[k] for k in sorted(insts_a.keys() - insts_b.keys())]

        changed: list[InstrumentChange] = []
        common_ids = sorted(insts_a.keys() & insts_b.keys())
        for cid in common_ids:
            a = insts_a[cid]
            b = insts_b[cid]
            field_diffs: dict[str, tuple[Any, Any]] = {}
            if a.lot_size != b.lot_size:
                field_diffs["lot_size"] = (a.lot_size, b.lot_size)
            if a.tick_size != b.tick_size:
                field_diffs["tick_size"] = (a.tick_size, b.tick_size)
            if a.expiry != b.expiry:
                field_diffs["expiry"] = (a.expiry, b.expiry)
            if a.symbol != b.symbol:
                field_diffs["symbol"] = (a.symbol, b.symbol)
            if a.isin != b.isin:
                field_diffs["isin"] = (a.isin, b.isin)

            if field_diffs:
                changed.append(InstrumentChange(id=InstrumentId(cid), changes=field_diffs))

        report = InstrumentDiffReport(
            source=source,
            date_a=date_a,
            date_b=date_b,
            added=added,
            removed=removed,
            changed=changed,
        )

        if journal is not None:
            try:
                journal.append(
                    "instruments.diff",
                    {
                        "source": source,
                        "date_a": date_a.isoformat(),
                        "date_b": date_b.isoformat(),
                        "added_count": len(added),
                        "removed_count": len(removed),
                        "changed_count": len(changed),
                    },
                )
            except Exception:
                pass

        return report
