"""Corporate actions store managing partitioned actions.parquet and manifest tracking."""

from __future__ import annotations

import dataclasses
import hashlib
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from algodesk.api.errors import DataError
from algodesk.api.market import ActionRecord
from algodesk.api.protocols import DataSource
from algodesk.api.services import JournalWriter, ParamReader
from algodesk.data.catalog import resolve_selector
from algodesk.data.limiter import RateLimiter
from algodesk.data.manifest import DataManifest, FileRecord

_NON_NULL_COLS = "id exchange symbol ex_date type text source source_ref fetched_at status".split()
_STR_NULL_COLS = "amount record_date issue_price reason verified_on".split()
_FLT_NULL_COLS = "ratio ratio_old ratio_new".split()
ACTION_SCHEMA = pa.schema(
    [pa.field(n, pa.string(), nullable=False) for n in _NON_NULL_COLS]
    + [pa.field(n, pa.float64(), nullable=True) for n in _FLT_NULL_COLS]
    + [pa.field(n, pa.string(), nullable=True) for n in _STR_NULL_COLS]
)


def _compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def _s_or_n(v: Any) -> str | None:
    return str(v) if v is not None else None


def _f_or_n(v: Any) -> float | None:
    return float(v) if v is not None else None


def _d_or_n(v: Any) -> Decimal | None:
    return Decimal(str(v)) if v is not None and str(v) != "" else None


def action_to_dict(a: ActionRecord) -> dict[str, Any]:
    """Convert ActionRecord to dictionary conforming to ACTION_SCHEMA."""
    return {
        "id": a.id,
        "exchange": a.exchange.upper(),
        "symbol": a.symbol.upper(),
        "ex_date": a.ex_date.isoformat(),
        "type": a.type.lower(),
        "ratio": _f_or_n(a.ratio),
        "ratio_old": _f_or_n(a.ratio_old),
        "ratio_new": _f_or_n(a.ratio_new),
        "amount": _s_or_n(a.amount),
        "text": a.text,
        "source": a.source,
        "source_ref": a.source_ref,
        "record_date": a.record_date.isoformat() if a.record_date else None,
        "fetched_at": a.fetched_at,
        "status": a.status,
        "issue_price": _s_or_n(a.issue_price),
        "reason": a.reason,
        "verified_on": a.verified_on,
    }


def row_to_action(row: dict[str, Any]) -> ActionRecord:
    """Reconstruct ActionRecord from parquet dictionary row."""
    g = row.get
    ex_d = date.fromisoformat(str(g("ex_date"))[:10]) if g("ex_date") else date(1970, 1, 1)
    rec_d = date.fromisoformat(str(g("record_date"))[:10]) if g("record_date") else None
    return ActionRecord(
        ex_d,
        str(g("type", "other")),
        _f_or_n(g("ratio")),
        _f_or_n(g("ratio_old")),
        _f_or_n(g("ratio_new")),
        _d_or_n(g("amount")),
        rec_d,
        str(g("text") or ""),
        str(g("source") or ""),
        str(g("source_ref") or ""),
        str(g("fetched_at") or ""),
        str(g("symbol") or ""),
        str(g("exchange") or "NSE"),
        str(g("status") or "unverified"),
        str(g("id") or ""),
        _d_or_n(g("issue_price")),
        str(g("reason") or ""),
        str(g("verified_on") or ""),
    )


class ActionsStore:
    """Manages storage and retrieval of corporate actions parquet files."""

    __slots__ = ("_data_dir", "_manifest")

    def __init__(self, data_dir: Path | str, manifest: DataManifest | None = None) -> None:
        self._data_dir = Path(data_dir)
        self._manifest = manifest

    @property
    def manifest(self) -> DataManifest | None:
        return self._manifest

    def actions_rel_path(self, exchange: str, symbol: str) -> str:
        """Return relative path for actions parquet file."""
        return f"actions/exchange={exchange.upper()}/symbol={symbol.upper()}/actions.parquet"

    def actions_path(self, exchange: str, symbol: str) -> Path:
        """Return absolute path for actions parquet file."""
        return self._data_dir / self.actions_rel_path(exchange, symbol)

    def read(self, exchange: str, symbol: str) -> list[ActionRecord]:
        """Read all corporate actions for symbol sorted by ex_date."""
        target = self.actions_path(exchange, symbol)
        if not target.exists():
            return []
        try:
            tbl = pq.read_table(target)
            pydict = tbl.to_pydict()
            if not pydict or "id" not in pydict:
                return []
            n = len(pydict["id"])
            rows = [{k: pydict[k][i] for k in pydict} for i in range(n)]
            records = [row_to_action(r) for r in rows]
            records.sort(key=lambda a: a.ex_date)
            return records
        except Exception:
            return []

    def write(
        self,
        exchange: str,
        symbol: str,
        actions: Sequence[ActionRecord],
        source: str = "",
    ) -> Path:
        """Merge actions by (source, ex_date, type) and atomically write parquet file."""
        exch = exchange.upper()
        sym = symbol.upper()
        target = self.actions_path(exch, sym)
        target.parent.mkdir(parents=True, exist_ok=True)

        existing = self.read(exch, sym)
        merged_map: dict[tuple[str, str, str], ActionRecord] = {
            (a.source.lower(), a.ex_date.isoformat(), a.type.lower()): a for a in existing
        }
        for act in actions:
            act_exch = act.exchange or exch
            act_sym = act.symbol or sym
            act_src = act.source or source
            if act.exchange != act_exch or act.symbol != act_sym or act.source != act_src:
                act = dataclasses.replace(act, exchange=act_exch, symbol=act_sym, source=act_src)
            merged_map[(act.source.lower(), act.ex_date.isoformat(), act.type.lower())] = act

        sorted_actions = sorted(merged_map.values(), key=lambda a: (a.ex_date, a.source, a.type))
        cols: dict[str, list[Any]] = {field.name: [] for field in ACTION_SCHEMA}
        for a in sorted_actions:
            d = action_to_dict(a)
            for k, v in d.items():
                cols[k].append(v)

        table = pa.Table.from_pydict(cols, schema=ACTION_SCHEMA)
        tmp_path = target.with_suffix(f".tmp.{uuid4().hex[:8]}")
        pq.write_table(table, tmp_path, compression="zstd")
        with open(tmp_path, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_path, target)

        if self._manifest is not None:
            self._record_manifest(exch, sym, target, sorted_actions, source)

        return target

    def _record_manifest(
        self,
        exchange: str,
        symbol: str,
        target_path: Path,
        actions: Sequence[ActionRecord],
        source: str,
    ) -> None:
        """Register written actions file in DataManifest under kind='actions'."""
        if self._manifest is None:
            return
        first_d = actions[0].ex_date.isoformat() if actions else ""
        last_d = actions[-1].ex_date.isoformat() if actions else ""
        rec = FileRecord(
            self.actions_rel_path(exchange, symbol),
            f"{exchange}:EQ:{symbol}",
            "actions",
            "actions",
            datetime.now(UTC).date().isoformat(),
            len(actions),
            target_path.stat().st_size,
            first_d,
            last_d,
            _compute_sha256(target_path),
            source or (actions[0].source if actions else "unknown"),
            "ready",
            datetime.now(UTC).isoformat(),
        )
        self._manifest.record_file(rec)


class CorporateActionsManager:
    """Coordinates corporate actions ingestion, caching, and TTL refresh."""

    __slots__ = ("_groups_dir", "_journal", "_limiter", "_params", "_sources", "_store")

    def __init__(
        self,
        store: ActionsStore,
        sources: Mapping[str, DataSource],
        limiter: RateLimiter | None = None,
        params: ParamReader | None = None,
        journal: JournalWriter | None = None,
        groups_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._sources = dict(sources)
        self._limiter = limiter or RateLimiter(params=params, journal=journal)
        self._params = params
        self._journal = journal
        self._groups_dir = groups_dir

    @property
    def store(self) -> ActionsStore:
        return self._store

    def _is_stale(self, existing: Sequence[ActionRecord], ttl_hours: int) -> bool:
        def _parse(s: str) -> datetime | None:
            try:
                dt = datetime.fromisoformat(s)
                return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
            except Exception:
                return None

        dts = [dt for a in existing if a.fetched_at and (dt := _parse(a.fetched_at))]
        return not dts or (datetime.now(UTC) - max(dts)).total_seconds() / 3600.0 >= ttl_hours

    def _resolve_sources(self, source: str | None) -> list[str]:
        if source:
            s_lower = source.strip().lower()
            if s_lower == "all":
                return [
                    n
                    for n, s in self._sources.items()
                    if hasattr(s, "actions") or hasattr(s, "corporate_actions")
                ]
            if s_lower not in self._sources:
                raise DataError(
                    what=f"Corporate actions source '{source}' is not configured",
                    where="CorporateActionsManager.refresh",
                    fix=f"Configure '{source}' or choose from {list(self._sources.keys())}",
                )
            return [s_lower]
        return [
            n
            for n, s in self._sources.items()
            if hasattr(s, "actions") or hasattr(s, "corporate_actions")
        ]

    def _parse_inst(self, sym_or_id: str) -> tuple[str, str]:
        s_str = sym_or_id.strip()
        parts = s_str.split(":") if ":" in s_str else ["NSE", "EQ", s_str]
        return parts[0].upper(), parts[-1].upper()

    def _fetch_and_store_symbol(
        self, exch: str, sym: str, src_list: list[str]
    ) -> tuple[int, dict[str, int], list[str]]:
        sym_actions: list[ActionRecord] = []
        counts: dict[str, int] = {}
        warnings: list[str] = []
        for src_name in src_list:
            self._limiter.acquire(src_name, "corporate_actions")
            try:
                src_obj = self._sources[src_name]
                if hasattr(src_obj, "actions"):
                    acts = list(src_obj.actions(sym))
                elif hasattr(src_obj, "corporate_actions"):
                    acts = list(src_obj.corporate_actions(sym))
                else:
                    acts = []
                self._store.write(exch, sym, acts, source=src_name)
                sym_actions.extend(acts)
                counts[src_name] = len(acts)
            except Exception as exc:
                msg = f"Source '{src_name}' actions fetch failed for {sym}: {exc}"
                warnings.append(msg)
                if len(src_list) == 1:
                    raise
        if self._journal:
            self._journal.append(
                "data.actions",
                {
                    "symbol": sym,
                    "exchange": exch,
                    "sources": src_list,
                    "count": len(sym_actions),
                    "action_ids": [a.id for a in sym_actions],
                },
            )
        return len(sym_actions), counts, warnings

    def refresh(
        self, selector: str, source: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        manifest = self._store.manifest
        parts = [s.strip() for s in selector.split(",") if s.strip()]
        insts: list[str] = []
        for part in parts:
            try:
                resolved = (
                    resolve_selector(part, manifest, groups_dir=self._groups_dir)
                    if manifest
                    else [part]
                )
            except Exception:
                resolved = [part]
            insts.extend(resolved or [part])
        insts = insts or parts or [selector]

        p = self._params
        ttl = int(p.get("data.actions_ttl_hours", "24")) if p else 24
        src_list = self._resolve_sources(source)

        # pyrefly: ignore[implicit-any-empty-container]
        refreshed, skipped, total = [], [], 0
        all_warnings: list[str] = []
        source_counts: dict[str, int] = {s: 0 for s in src_list}
        for sym_or_id in insts:
            # pyrefly: ignore[unnecessary-type-conversion]
            exch, sym = self._parse_inst(str(sym_or_id))
            if not force and not self._is_stale(self._store.read(exch, sym), ttl):
                skipped.append(f"{exch}:{sym}")
                continue

            cnt, src_map, warns = self._fetch_and_store_symbol(exch, sym, src_list)
            all_warnings.extend(warns)
            for s_name, c in src_map.items():
                source_counts[s_name] = source_counts.get(s_name, 0) + c
            refreshed.append(f"{exch}:{sym}")
            total += cnt

        return {
            "refreshed": refreshed,
            "skipped": skipped,
            "actions_count": total,
            "sources": src_list,
            "source_counts": source_counts,
            "warnings": all_warnings,
        }
