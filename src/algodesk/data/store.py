"""Historical bar store with atomic commits, day-level coverage, and provenance.

Conforms to M2 design:
- 1m = data/bars/source=S/timeframe=1m/exchange=E/segment=G/symbol=X/adj=A/year=YYYY/YYYY-MM.parquet
- 1d = data/bars/source=S/timeframe=1d/exchange=E/segment=G/symbol=X/adj=A/daily.parquet
- Per-file read, merge by timestamp, temp file, fsync, atomic rename, manifest transaction.
- Idempotent: identical input chunk produces identical sha256 checksum.
- Complete day preservation unless refresh=True (journaled).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from algodesk.api.errors import DataError
from algodesk.api.market import (
    Bar,
    CoverageStatus,
    DataSlice,
    SeriesKey,
)
from algodesk.api.protocols import Calendar
from algodesk.api.services import JournalWriter, ParamReader
from algodesk.data.coverage import CoverageIndex
from algodesk.data.layout import (
    bar_file_path,
    dataset_id_for_key,
)
from algodesk.data.manifest import (
    CoverageDayRecord,
    DataManifest,
    DatasetRecord,
    PartRecord,
)
from algodesk.data.schema import (
    read_bars_parquet,
    write_bars_parquet,
)

_DEFAULT_EXCH_TZ = ZoneInfo("Asia/Kolkata")
_BASE_TIMEFRAMES = ("1m", "1d")


def _compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class BarStore:
    """Manages transactional storage, retrieval, and day-level coverage of market bars."""

    __slots__ = (
        "_calendar",
        "_coverage_index",
        "_data_dir",
        "_exchange_tz",
        "_journal",
        "_manifest",
        "_params",
        "_settle_delay_m",
    )

    def __init__(
        self,
        data_dir: Path | str,
        manifest: DataManifest,
        params: ParamReader | None = None,
        journal: JournalWriter | None = None,
        calendar: Calendar | None = None,
        exchange_tz: tzinfo | None = None,
        settle_minutes: int | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._manifest = manifest
        self._params = params
        self._journal = journal
        self._calendar = calendar
        self._exchange_tz = exchange_tz or _DEFAULT_EXCH_TZ

        self._settle_delay_m = (
            settle_minutes
            if settle_minutes is not None
            else (int(params.get("data.settle_minutes", "0")) if params else 0)
        )

        self._coverage_index = CoverageIndex(
            manifest=self._manifest,
            calendar=self._calendar,
            settle_minutes=self._settle_delay_m,
        )

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def manifest(self) -> DataManifest:
        return self._manifest

    @property
    def coverage_index(self) -> CoverageIndex:
        return self._coverage_index

    @property
    def exchange_tz(self) -> tzinfo:
        return self._exchange_tz

    def dataset_id(self, key: SeriesKey) -> str:
        return dataset_id_for_key(key)

    def commit(
        self,
        key: SeriesKey,
        bars: Sequence[Bar],
        refresh: bool = False,
        source_version: str = "1.0",
        validator: object | None = None,
    ) -> list[PartRecord]:
        """Atomically commit a chunk of bars with day-level coverage tracking and idempotence."""
        if not bars:
            return []

        if key.timeframe not in _BASE_TIMEFRAMES:
            tf_list = ", ".join(_BASE_TIMEFRAMES)
            raise DataError(
                what=(
                    f"Unsupported bar timeframe '{key.timeframe}'. "
                    f"Stored bar timeframes are limited to base timeframes ({tf_list})"
                ),
                where="BarStore.commit",
                fix=f"Store base timeframe ({tf_list}) or use resample() for derived intervals",
            )

        val_obj = validator
        if val_obj is None:
            from algodesk.data.validator import BarValidator

            val_obj = BarValidator(
                data_dir=self._data_dir,
                manifest=self._manifest,
                calendar=self._calendar,
                params=self._params,
            )
        if hasattr(val_obj, "validate_chunk"):
            v_rep = val_obj.validate_chunk(bars, key)
            if hasattr(v_rep, "clean_bars") and v_rep.clean_bars is not None:
                bars = v_rep.clean_bars
        if not bars:
            return []

        ds_id = self.dataset_id(key)
        now_iso = datetime.now(UTC).isoformat()

        # Ensure dataset metadata exists in manifest with authoritative adjustment_basis
        if key.source == "upstox":
            basis = "splits"
        elif key.source == "yfinance":
            basis = "total" if key.adjustment == "adjusted" else "splits"
        else:
            basis = "splits" if key.adjustment == "raw" else "total"

        self._manifest.record_dataset(
            DatasetRecord(
                dataset_id=ds_id,
                instrument=str(key.instrument),
                kind=key.kind,
                timeframe=key.timeframe,
                adjustment=key.adjustment,
                source=key.source,
                schema_version=1,
                created_at=now_iso,
                actions_epoch=None,
                adjustment_basis=basis,
            )
        )

        # Inspect incoming bars by trading date in exchange timezone
        day_to_bars: dict[date, list[Bar]] = {}
        for b in bars:
            d = b.start_ts.astimezone(self._exchange_tz).date()
            day_to_bars.setdefault(d, []).append(b)

        # Check existing coverage for these days
        existing_cov = {
            r.day: r
            for r in self._manifest.get_coverage_days(
                ds_id,
                start_day=min(day_to_bars.keys()).isoformat(),
                end_day=max(day_to_bars.keys()).isoformat(),
            )
        }

        # Filter out bars for COMPLETE days if refresh=False
        days_to_commit: set[date] = set()
        for day in day_to_bars:
            day_str = day.isoformat()
            cov_rec = existing_cov.get(day_str)
            if cov_rec is not None and cov_rec.status == CoverageStatus.COMPLETE and not refresh:
                continue

            days_to_commit.add(day)
            if cov_rec is not None and cov_rec.status == CoverageStatus.COMPLETE and refresh:
                if self._journal is not None:
                    self._journal.append(
                        kind="data.refresh",
                        payload={
                            "dataset_id": ds_id,
                            "instrument": str(key.instrument),
                            "day": day_str,
                            "source": key.source,
                        },
                    )

        # If no days need writing (e.g. all days are already COMPLETE and refresh=False),
        # return the existing part records for the files corresponding to the input bars.
        if not days_to_commit:
            existing_parts: list[PartRecord] = []
            seen_paths: set[str] = set()
            for b in bars:
                target_file = bar_file_path(self._data_dir, key, b.start_ts)
                try:
                    rel_path = str(target_file.relative_to(self._data_dir))
                except ValueError:
                    rel_path = str(target_file)
                if rel_path not in seen_paths:
                    seen_paths.add(rel_path)
                    part = self._manifest.get_part_by_path(rel_path)
                    if part is not None:
                        existing_parts.append(part)
            return existing_parts

        # Group bars to commit by destination file path
        bars_by_file: dict[Path, list[Bar]] = {}
        for day in days_to_commit:
            for b in day_to_bars[day]:
                p = bar_file_path(self._data_dir, key, b.start_ts)
                bars_by_file.setdefault(p, []).append(b)

        part_records: list[PartRecord] = []
        coverage_updates: list[CoverageDayRecord] = []

        for target_file, new_bars in bars_by_file.items():
            target_file.parent.mkdir(parents=True, exist_ok=True)

            # Read existing bars from file if it exists
            existing_bars: list[Bar] = []
            if target_file.exists():
                existing_bars = read_bars_parquet(
                    target_file,
                    key.instrument,
                    key.timeframe,
                )

            # Merge existing bars and new bars
            merged_dict: dict[datetime, Bar] = {b.start_ts: b for b in existing_bars}
            for b in new_bars:
                merged_dict[b.start_ts] = b

            merged_bars = sorted(merged_dict.values(), key=lambda b: b.start_ts)

            # Write to temporary file in same folder, fsync, and atomic rename
            tmp_file = target_file.with_name(f".{target_file.name}.tmp_{uuid4().hex}")
            write_bars_parquet(tmp_file, merged_bars, exchange_tz=self._exchange_tz)

            # Fsync to ensure durability before atomic rename
            with open(tmp_file, "rb") as f_tmp:
                f_tmp.flush()
                os.fsync(f_tmp.fileno())

            # Atomic rename replaces target_file
            tmp_file.replace(target_file)

            # Compute metadata
            sha256 = _compute_sha256(target_file)
            file_bytes = target_file.stat().st_size
            first_ts = merged_bars[0].start_ts.isoformat()
            last_ts = merged_bars[-1].start_ts.isoformat()
            try:
                rel_path = str(target_file.relative_to(self._data_dir))
            except ValueError:
                rel_path = str(target_file)

            part_rec = PartRecord(
                rel_path,
                ds_id,
                len(merged_bars),
                file_bytes,
                first_ts,
                last_ts,
                sha256,
                key.source,
                source_version,
                now_iso,
            )
            self._manifest.record_part(part_rec)
            part_records.append(part_rec)

            if self._journal is not None:
                self._journal.append(
                    kind="data.commit",
                    payload={
                        "dataset_id": ds_id,
                        "path": rel_path,
                        "rows": len(merged_bars),
                        "sha256": sha256,
                        "refresh": refresh,
                    },
                )

        # Update coverage_days for all affected days
        for day in days_to_commit:
            d_bars = day_to_bars[day]
            st = self._coverage_index.evaluate_day_status(
                dataset_id=ds_id,
                day=day,
                has_bars=len(d_bars) > 0,
            )
            part_path = part_records[0].path if part_records else ""
            coverage_updates.append(
                CoverageDayRecord(
                    ds_id,
                    day.isoformat(),
                    st,
                    len(d_bars),
                    len(d_bars),
                    part_path,
                    now_iso,
                )
            )

        if coverage_updates:
            self._manifest.record_coverage_days(coverage_updates)

        return part_records

    def read(
        self,
        key: SeriesKey,
        start: datetime,
        end: datetime,
    ) -> DataSlice:
        """Read historical bars slice with gap detection and dataset provenance."""
        ds_id = self.dataset_id(key)
        start_day = start.astimezone(self._exchange_tz).date()
        end_day = end.astimezone(self._exchange_tz).date()

        # Collect files spanning the date range
        files_to_read: set[Path] = set()
        cur_day = start_day
        while cur_day <= end_day:
            cur_dt = datetime(cur_day.year, cur_day.month, cur_day.day, 10, 0, tzinfo=UTC)
            p = bar_file_path(self._data_dir, key, cur_dt)
            if p.exists():
                files_to_read.add(p)
            cur_day += timedelta(days=1)

        all_bars: list[Bar] = []
        checksums: list[str] = []

        for f in sorted(files_to_read):
            bars = read_bars_parquet(f, key.instrument, key.timeframe)
            all_bars.extend(bars)
            try:
                rel = str(f.relative_to(self._data_dir))
            except ValueError:
                rel = str(f)
            p_rec = self._manifest.get_part_by_path(rel)
            if p_rec is not None:
                checksums.append(p_rec.sha256)
            else:
                checksums.append(_compute_sha256(f))

        # Filter strictly within [start, end]
        if key.timeframe == "1d":
            s_date = start.astimezone(self._exchange_tz).date()
            e_date = end.astimezone(self._exchange_tz).date()
            filtered_bars = [
                b
                for b in all_bars
                if s_date <= b.start_ts.astimezone(self._exchange_tz).date() <= e_date
            ]
        else:
            filtered_bars = [b for b in all_bars if start <= b.start_ts <= end]
        filtered_bars.sort(key=lambda b: b.start_ts)

        # Detect gaps
        gaps = self._coverage_index.detect_gaps(ds_id, start_day, end_day)

        return DataSlice(
            key=key,
            start=start,
            end=end,
            bars=tuple(filtered_bars),
            gaps=tuple(gaps),
            dataset_ids=(ds_id,),
            checksums=tuple(checksums),
        )
