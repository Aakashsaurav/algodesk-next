"""Migration of legacy instrument identifiers and partitioned storage layout.

Handles copy-first, verified, resumable migration of legacy tick and bar
partitions to contract-aware partitions (e.g. MCX:FNO:GOLD FUT... -> MCX:FUT:GOLD:...).
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from algodesk.api.errors import DataError
from algodesk.api.market import InstrumentId
from algodesk.data.aliases import resolve_alias
from algodesk.data.layout import (
    bar_file_path,
    parse_bar_file_path,
    parse_tick_file_path,
    tick_file_path,
)
from algodesk.data.manifest import DataManifest, FileRecord


@dataclass(frozen=True, slots=True)
class MigrationItem:
    source_path: Path
    target_path: Path
    legacy_id: str
    canonical_id: InstrumentId
    rows: int
    bytes: int
    sha256: str
    kind: str
    timeframe: str
    date_val: str
    first_ts: str
    last_ts: str
    source_name: str


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    items: tuple[MigrationItem, ...]

    @property
    def total_files(self) -> int:
        return len(self.items)

    @property
    def total_rows(self) -> int:
        return sum(item.rows for item in self.items)

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_files": self.total_files,
            "total_rows": self.total_rows,
            "total_bytes": self.total_bytes,
            "items": [
                {
                    "legacy_id": item.legacy_id,
                    "canonical_id": item.canonical_id,
                    "source_path": str(item.source_path),
                    "target_path": str(item.target_path),
                    "rows": item.rows,
                    "bytes": item.bytes,
                    "sha256": item.sha256,
                }
                for item in self.items
            ],
        }


def build_migration_plan(data_dir: Path, manifest: DataManifest) -> MigrationPlan:
    """Scan manifest and disk for legacy instrument partitions requiring migration."""
    items: list[MigrationItem] = []
    seen_sources: set[str] = set()

    # 1. Inspect manifest files
    for f in manifest.get_files():
        inst = f.instrument
        canonical = resolve_alias(inst)
        p = Path(f.path)
        is_legacy = (
            str(canonical) != inst or " " in str(p) or "/segment=FNO/" in str(p).replace("\\", "/")
        )
        if not is_legacy:
            continue

        if not p.is_file():
            continue

        source_str = str(p.resolve())
        if source_str in seen_sources:
            continue

        if f.kind == "ticks":
            target = tick_file_path(data_dir, canonical, f.date, filename=p.name)
        else:
            # Bars: parse bar path info if possible
            target = p  # fallback
            b_info = parse_bar_file_path(p)
            if b_info:
                # Resolve target path for bars
                from algodesk.api.market import SeriesKey

                sk = SeriesKey(
                    instrument=canonical,
                    kind="bars",
                    timeframe=f.timeframe,
                    adjustment=b_info.get("adj", "raw"),
                    source=f.source,
                )
                target = bar_file_path(data_dir, sk, datetime.fromisoformat(f.date))

        if p.resolve() == target.resolve():
            continue

        seen_sources.add(source_str)
        items.append(
            MigrationItem(
                source_path=p,
                target_path=target,
                legacy_id=inst,
                canonical_id=canonical,
                rows=f.rows,
                bytes=f.bytes,
                sha256=f.sha256,
                kind=f.kind,
                timeframe=f.timeframe,
                date_val=f.date,
                first_ts=f.first_ts,
                last_ts=f.last_ts,
                source_name=f.source,
            )
        )

    # 2. Inspect on-disk ticks that might not be in manifest
    ticks_dir = data_dir / "ticks"
    if ticks_dir.is_dir():
        for p in ticks_dir.rglob("*.parquet"):
            source_str = str(p.resolve())
            if source_str in seen_sources:
                continue
            if "segment=FNO" in str(p).replace("\\", "/") or " " in p.name or " " in str(p):
                t_info = parse_tick_file_path(p)
                if not t_info:
                    # Parse from path segments
                    parts = p.parts
                    seg, exch, sym, d_val = "FNO", "MCX", "", ""
                    for elem in parts:
                        if elem.startswith("exchange="):
                            exch = elem.split("=")[1]
                        elif elem.startswith("segment="):
                            seg = elem.split("=")[1]
                        elif elem.startswith("symbol="):
                            sym = elem.split("=")[1]
                        elif elem.startswith("date="):
                            d_val = elem.split("=")[1]
                    raw_id = f"{exch}:{seg}:{sym}"
                    canonical = resolve_alias(raw_id)
                else:
                    raw_id = str(t_info.get("instrument", ""))
                    canonical = resolve_alias(raw_id)
                    d_val = str(t_info.get("date", ""))

                target = tick_file_path(data_dir, canonical, d_val, filename=p.name)
                if p.resolve() == target.resolve():
                    continue

                try:
                    pf = pq.ParquetFile(p)
                    rows = pf.metadata.num_rows
                except Exception:
                    rows = 0
                sha = hashlib.sha256(p.read_bytes()).hexdigest()

                seen_sources.add(source_str)
                items.append(
                    MigrationItem(
                        source_path=p,
                        target_path=target,
                        legacy_id=raw_id,
                        canonical_id=canonical,
                        rows=rows,
                        bytes=p.stat().st_size,
                        sha256=sha,
                        kind="ticks",
                        timeframe="tick",
                        date_val=d_val,
                        first_ts="",
                        last_ts="",
                        source_name="live",
                    )
                )

    return MigrationPlan(items=tuple(items))


def apply_migration(
    plan: MigrationPlan,
    manifest: DataManifest,
    journal: Any | None = None,
) -> int:
    """Execute copy-first migration step.

    Copies each legacy file to target partition, verifies row count and checksum,
    and updates manifest in one transaction per file. Retains source files.
    """
    copied_count = 0
    for item in plan.items:
        # 1. Target directory
        item.target_path.parent.mkdir(parents=True, exist_ok=True)

        # 2. Copy or verify existing target copy
        if item.target_path.is_file():
            target_sha = hashlib.sha256(item.target_path.read_bytes()).hexdigest()
            target_rows = pq.ParquetFile(item.target_path).metadata.num_rows
            if target_sha != item.sha256 or (item.rows > 0 and target_rows != item.rows):
                item.target_path.unlink(missing_ok=True)
                shutil.copy2(item.source_path, item.target_path)
        else:
            shutil.copy2(item.source_path, item.target_path)

        # 3. Verify
        target_sha = hashlib.sha256(item.target_path.read_bytes()).hexdigest()
        target_rows = pq.ParquetFile(item.target_path).metadata.num_rows
        if target_sha != item.sha256:
            item.target_path.unlink(missing_ok=True)
            raise DataError(
                what=f"Migration checksum mismatch for {item.source_path} -> {item.target_path}",
                where="migration.apply_migration",
                fix="Inspect source parquet file integrity and re-run",
            )
        if item.rows > 0 and target_rows != item.rows:
            item.target_path.unlink(missing_ok=True)
            raise DataError(
                what=(
                    f"Migration row count mismatch for {item.source_path} "
                    f"({item.rows} vs {target_rows})"
                ),
                where="migration.apply_migration",
                fix="Inspect source parquet file integrity and re-run",
            )

        # 4. Manifest update in one transaction per file
        manifest.add_alias(item.legacy_id, str(item.canonical_id))
        rec = FileRecord(
            path=str(item.target_path.resolve()),
            instrument=str(item.canonical_id),
            kind=item.kind,
            timeframe=item.timeframe,
            date=item.date_val,
            rows=target_rows,
            bytes=item.target_path.stat().st_size,
            first_ts=item.first_ts,
            last_ts=item.last_ts,
            sha256=target_sha,
            source=item.source_name,
            state="live",
            created_ts=datetime.now(UTC).isoformat(),
        )
        manifest.add_file(rec)

        if journal is not None:
            try:
                journal.append(
                    "data.migrate_ids.file",
                    {
                        "source": str(item.source_path),
                        "target": str(item.target_path),
                        "legacy_id": item.legacy_id,
                        "canonical_id": str(item.canonical_id),
                        "rows": target_rows,
                        "sha256": target_sha,
                    },
                )
            except Exception:
                pass

        copied_count += 1

    return copied_count


def finalize_migration(
    plan: MigrationPlan,
    manifest: DataManifest,
    journal: Any | None = None,
) -> int:
    """Finalize migration by deleting legacy copies only after verification."""
    finalized_count = 0
    for item in plan.items:
        if not item.target_path.is_file():
            raise DataError(
                what=f"Cannot finalize: target file {item.target_path} is missing",
                where="migration.finalize_migration",
                fix="Re-run 'algodesk data migrate-ids --apply' before finalizing",
            )
        target_sha = hashlib.sha256(item.target_path.read_bytes()).hexdigest()
        if target_sha != item.sha256:
            raise DataError(
                what=f"Cannot finalize: target file checksum mismatch for {item.target_path}",
                where="migration.finalize_migration",
                fix="Re-run 'algodesk data migrate-ids --apply' before finalizing",
            )

        # Remove old file and remove from manifest
        try:
            item.source_path.unlink(missing_ok=True)
        except OSError:
            pass

        manifest.delete_file(str(item.source_path.resolve()))

        # Clean empty parent directories up to ticks / bars
        parent = item.source_path.parent
        while parent != parent.parent and parent.name not in ("ticks", "bars"):
            try:
                if not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
                else:
                    break
            except OSError:
                break

        if journal is not None:
            try:
                journal.append(
                    "data.migrate_ids.finalized",
                    {
                        "removed_source": str(item.source_path),
                        "target": str(item.target_path),
                        "canonical_id": str(item.canonical_id),
                    },
                )
            except Exception:
                pass

        finalized_count += 1

    return finalized_count
