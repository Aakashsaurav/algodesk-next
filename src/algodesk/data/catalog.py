"""Data catalog providing coverage inspection, selector resolution, and safe deletion.

Implements coverage queries across partitioned files, flexible symbol/group resolution
per design §4.4, and two-stage transactional deletion with safety trash.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from algodesk.api.errors import ConfigError, DataError
from algodesk.api.market import parse_id
from algodesk.data.groups import get_group, load_groups
from algodesk.data.manifest import DataManifest, FileRecord


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """Aggregated coverage summary row."""

    target: str
    kind: str
    timeframe: str
    first_date: str
    last_date: str
    days: int
    rows: int
    bytes: int
    source: str


@dataclass(frozen=True, slots=True)
class DeletePlan:
    """Prepared deletion plan for user confirmation."""

    selectors: tuple[str, ...]
    instruments: tuple[str, ...]
    files: tuple[FileRecord, ...]
    first_date: str
    last_date: str
    total_files: int
    total_rows: int
    total_bytes: int
    no_data_instruments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TrashItem:
    """Safety trash directory entry."""

    name: str
    path: str
    deleted_at: str
    reason: str
    files_count: int
    bytes_count: int


def _normalize_name(name: str) -> str:
    """Normalize symbol/contract name for fuzzy comparison."""
    return re.sub(r"[\W_]+", "", name).upper()


def _check_journal_lock(lock_path: Path) -> None:
    """Verify that the journal writer lock is not held by another process."""
    if not lock_path.exists():
        return
    try:
        with open(lock_path, "a") as f:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except (BlockingIOError, OSError) as exc:
        raise ConfigError(
            what=f"Refusing deletion while journal writer lock is held: {exc}",
            where=str(lock_path),
            fix="Stop running engine or wait for active session before deleting data",
        ) from exc


def _extract_symbol(inst_id: str) -> str:
    try:
        return parse_id(inst_id.upper()).symbol
    except Exception:
        parts = inst_id.split(":")
        return parts[-1] if parts else inst_id


def resolve_selector(
    selector: str,
    manifest: DataManifest,
    groups_dir: Path | str | None = None,
) -> list[str]:
    """Resolve a single selector expression to one or more canonical instrument IDs."""
    sel = selector.strip()
    if not sel:
        return []

    if sel.startswith("@"):
        group_name = sel[1:]
        members = get_group(group_name, groups_dir)
        if members is None:
            all_groups = list(load_groups(groups_dir).keys())
            close = difflib.get_close_matches(group_name, all_groups, n=3, cutoff=0.5)
            fix_msg = f"Available groups: {', '.join(all_groups)}"
            if close:
                fix_msg = f"Did you mean '@{close[0]}'? {fix_msg}"
            raise DataError(
                what=f"Unknown group selector '{sel}'", where="catalog.resolve", fix=fix_msg
            )
        return members

    known_instruments = manifest.list_instruments()
    all_inst_ids = [inst.id for inst in known_instruments]
    if not all_inst_ids:
        all_inst_ids = sorted({f.instrument for f in manifest.get_files()})
    for ds in manifest.list_datasets():
        if ds.instrument not in all_inst_ids:
            all_inst_ids.append(ds.instrument)

    if sel in all_inst_ids:
        return [sel]

    if "*" in sel or "?" in sel:
        matched = [i for i in all_inst_ids if fnmatch.fnmatch(i, sel)]
        if not matched:
            raise DataError(
                what=f"Glob selector '{sel}' matched zero instruments",
                where="catalog.resolve",
                fix=f"Known instruments: {', '.join(all_inst_ids[:10])}",
            )
        return sorted(matched)

    # Check exact match on broker trading symbol
    tsym_matches = [
        inst.id for inst in known_instruments if inst.trading_symbol.upper() == sel.upper()
    ]
    if len(tsym_matches) == 1:
        return tsym_matches
    if len(tsym_matches) > 1:
        raise DataError(
            what=f"Ambiguous trading symbol '{sel}' matches multiple instruments",
            where="catalog.resolve",
            fix=f"Specify full canonical ID: {', '.join(tsym_matches)}",
        )

    # Check bare symbol
    matched_bare = [i for i in all_inst_ids if _extract_symbol(i).upper() == sel.upper()]
    if len(matched_bare) == 1:
        return matched_bare
    if len(matched_bare) > 1:
        raise DataError(
            what=f"Ambiguous bare symbol '{sel}' matches multiple exchanges/segments",
            where="catalog.resolve",
            fix=f"Specify full canonical ID: {', '.join(matched_bare)}",
        )

    # Check compact contract name (ignoring case, spaces, punctuation)
    norm_sel = _normalize_name(sel)
    compact_matches = [
        inst.id
        for inst in known_instruments
        if _normalize_name(inst.trading_symbol) == norm_sel or _normalize_name(inst.id) == norm_sel
    ]
    if len(compact_matches) == 1:
        return compact_matches
    if len(compact_matches) > 1:
        raise DataError(
            what=f"Ambiguous compact contract selector '{sel}' matches multiple instruments",
            where="catalog.resolve",
            fix=f"Candidates: {', '.join(compact_matches)}",
        )

    # Not found, find closest candidates
    search_pool = [inst.trading_symbol for inst in known_instruments] + all_inst_ids
    candidates = difflib.get_close_matches(sel, search_pool, n=5, cutoff=0.3)
    fix_text = (
        f"Candidates: {', '.join(candidates)}"
        if candidates
        else "Check symbol with 'algodesk data list'"
    )
    raise DataError(what=f"Unknown selector '{sel}'", where="catalog.resolve", fix=fix_text)


def coverage(
    manifest: DataManifest,
    group_by: str | None = None,
    filters: dict[str, Any] | None = None,
    groups_dir: Path | str | None = None,
) -> list[CoverageRow]:
    """Calculate aggregated coverage summary over recorded files and bar parts."""
    files = list(
        manifest.get_files(
            instrument=filters.get("instrument") if filters else None,
            date_str=filters.get("date") if filters else None,
            kind=filters.get("kind") if filters else None,
        )
    )

    datasets = {d.dataset_id: d for d in manifest.list_datasets()}
    for p in manifest.get_parts():
        ds = datasets.get(p.dataset_id)
        if ds is None:
            continue
        if filters:
            if filters.get("instrument") and ds.instrument != filters["instrument"]:
                continue
            if filters.get("kind") and ds.kind != filters["kind"]:
                continue
            d_val = p.first_ts[:10] if p.first_ts else ""
            if filters.get("date") and d_val != filters["date"]:
                continue
        files.append(
            FileRecord(
                path=p.path,
                instrument=ds.instrument,
                kind=ds.kind,
                timeframe=ds.timeframe,
                date=p.first_ts[:10] if p.first_ts else "",
                rows=p.rows,
                bytes=p.bytes,
                first_ts=p.first_ts,
                last_ts=p.last_ts,
                sha256=p.sha256,
                source=p.source,
                state="bar",
                created_ts=p.fetched_at,
            )
        )

    if not files:
        return []

    groups = load_groups(groups_dir)
    inst_to_group: dict[str, str] = {}
    for gname, members in groups.items():
        for m in members:
            inst_to_group[m] = gname

    buckets: dict[tuple[str, str, str, str], list[FileRecord]] = {}
    for f in files:
        target = f.instrument
        if group_by == "exchange":
            target = parse_id(f.instrument).exchange
        elif group_by == "segment":
            target = parse_id(f.instrument).segment
        elif group_by == "underlying":
            target = parse_id(f.instrument).symbol
        elif group_by == "group":
            target = inst_to_group.get(f.instrument, "ungrouped")

        key = (target, f.kind, f.timeframe, f.source)
        buckets.setdefault(key, []).append(f)

    rows: list[CoverageRow] = []
    for (target, kind, tf, src), file_list in sorted(buckets.items()):
        dates = sorted({rec.date for rec in file_list})
        total_rows = sum(rec.rows for rec in file_list)
        total_bytes = sum(rec.bytes for rec in file_list)
        rows.append(
            CoverageRow(
                target=target,
                kind=kind,
                timeframe=tf,
                first_date=dates[0],
                last_date=dates[-1],
                days=len(dates),
                rows=total_rows,
                bytes=total_bytes,
                source=src,
            )
        )
    return rows


def plan_delete(
    selectors: Sequence[str],
    manifest: DataManifest,
    from_date: str | None = None,
    to_date: str | None = None,
    kind: str | None = None,
    groups_dir: Path | str | None = None,
) -> DeletePlan:
    """Build a deletion plan for the specified selectors and date filters."""
    resolved_insts: set[str] = set()
    for sel in selectors:
        for inst in resolve_selector(sel, manifest, groups_dir):
            resolved_insts.add(inst)

    matching_files: list[FileRecord] = []
    insts_with_files: set[str] = set()
    datasets = {d.dataset_id: d for d in manifest.list_datasets()}
    all_parts = manifest.get_parts()

    for inst in sorted(resolved_insts):
        files = manifest.get_files(instrument=inst, kind=kind)
        for f in files:
            if from_date and f.date < from_date:
                continue
            if to_date and f.date > to_date:
                continue
            matching_files.append(f)
            insts_with_files.add(inst)

        # Also match bar parts for this instrument
        for p in all_parts:
            ds = datasets.get(p.dataset_id)
            if ds is None or ds.instrument != inst:
                continue
            if kind and ds.kind != kind:
                continue
            p_date = p.first_ts[:10] if p.first_ts else ""
            if from_date and p_date and p_date < from_date:
                continue
            if to_date and p_date and p_date > to_date:
                continue
            matching_files.append(
                FileRecord(
                    path=p.path,
                    instrument=ds.instrument,
                    kind=ds.kind,
                    timeframe=ds.timeframe,
                    date=p_date,
                    rows=p.rows,
                    bytes=p.bytes,
                    first_ts=p.first_ts,
                    last_ts=p.last_ts,
                    sha256=p.sha256,
                    source=p.source,
                    state="bar",
                    created_ts=p.fetched_at,
                )
            )
            insts_with_files.add(inst)

    no_data_insts = sorted(resolved_insts - insts_with_files)

    dates = sorted({f.date for f in matching_files})
    first_d = dates[0] if dates else ""
    last_d = dates[-1] if dates else ""
    total_rows = sum(f.rows for f in matching_files)
    total_bytes = sum(f.bytes for f in matching_files)

    return DeletePlan(
        selectors=tuple(selectors),
        instruments=tuple(sorted(insts_with_files)),
        files=tuple(matching_files),
        first_date=first_d,
        last_date=last_d,
        total_files=len(matching_files),
        total_rows=total_rows,
        total_bytes=total_bytes,
        no_data_instruments=tuple(no_data_insts),
    )


def execute_delete(
    plan: DeletePlan,
    manifest: DataManifest,
    data_dir: Path | str,
    purge: bool = False,
    trash_days: int | None = None,
    reason: str = "",
    lock_path: Path | None = None,
) -> int:
    """Execute deletion plan moving files to trash or purging immediately."""
    if not plan.files:
        return 0

    if lock_path is not None:
        _check_journal_lock(lock_path)

    d_dir = Path(data_dir)
    trash_base = d_dir / ".trash"

    if purge:
        for f in plan.files:
            p = Path(f.path)
            if p.is_file():
                p.unlink()
    else:
        ts_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        trash_dir = trash_base / ts_str
        trash_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "deleted_at": datetime.now(UTC).isoformat(),
            "reason": reason,
            "selectors": plan.selectors,
            "instruments": plan.instruments,
            "files_count": len(plan.files),
            "bytes": plan.total_bytes,
        }
        (trash_dir / ".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        for f in plan.files:
            src_file = Path(f.path)
            if src_file.is_file():
                rel = (
                    src_file.relative_to(d_dir)
                    if src_file.is_relative_to(d_dir)
                    else Path(src_file.name)
                )
                dest = trash_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_file), str(dest))

    # Transactionally remove from manifest
    for f in plan.files:
        manifest.delete_file(f.path)
        manifest.delete_part(f.path)

    # Purge expired trash if requested
    if trash_days is not None and trash_days > 0:
        purge_trash(data_dir, older_than_days=trash_days)

    return len(plan.files)


def list_trash(data_dir: Path | str) -> list[TrashItem]:
    """List all safety trash folders and their metadata."""
    trash_base = Path(data_dir) / ".trash"
    if not trash_base.is_dir():
        return []

    items: list[TrashItem] = []
    for entry in sorted(trash_base.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        meta_file = entry / ".meta.json"
        meta: dict[str, Any] = {}
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        total_bytes = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        file_count = sum(1 for f in entry.rglob("*") if f.is_file() and f.name != ".meta.json")

        items.append(
            TrashItem(
                name=entry.name,
                path=str(entry),
                deleted_at=str(meta.get("deleted_at", entry.name)),
                reason=str(meta.get("reason", "manual deletion")),
                files_count=int(meta.get("files_count", file_count)),
                bytes_count=total_bytes,
            )
        )
    return items


def purge_trash(data_dir: Path | str, older_than_days: int | None = None) -> int:
    """Purge trash directories older than threshold, or all if older_than_days is None."""
    trash_base = Path(data_dir) / ".trash"
    if not trash_base.is_dir():
        return 0

    cutoff = (
        datetime.now(UTC) - timedelta(days=older_than_days) if older_than_days is not None else None
    )
    purged = 0

    for entry in trash_base.iterdir():
        if not entry.is_dir():
            continue
        if cutoff is not None:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
            if mtime > cutoff:
                continue
        shutil.rmtree(entry)
        purged += 1

    return purged
