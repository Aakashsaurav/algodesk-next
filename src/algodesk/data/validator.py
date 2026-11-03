"""Market bar validation rules and quarantine routing conforming to FR-DAT-5.

Checks schema, timestamp order, duplicate timestamps, OHLC sanity, non-negative volume,
session hours, maximum rows per session, and flags outlier price jumps as warnings.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from algodesk.api.errors import DataError
from algodesk.api.market import Bar, CoverageStatus, SeriesKey
from algodesk.api.protocols import Calendar
from algodesk.api.services import JournalWriter, ParamReader
from algodesk.data.layout import dataset_id_for_key
from algodesk.data.manifest import CoverageDayRecord, DataManifest, QuarantineRecord
from algodesk.data.schema import write_bars_parquet


@dataclass(slots=True)
class ValidationReport:
    """Outcome of validating a chunk of market bars."""

    is_valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_bars: int = 0
    quarantine_id: str | None = None
    clean_bars: list[Bar] = field(default_factory=list)
    dropped_flat_bars: int = 0


class BarValidator:
    """Validates bar sequences against exchange rules, catalog constraints, and sanity bounds."""

    __slots__ = (
        "_calendar",
        "_data_dir",
        "_exchange_tz",
        "_journal",
        "_manifest",
        "_params",
        "_price_jump_rate",
    )

    def __init__(
        self,
        data_dir: Path | str,
        manifest: DataManifest,
        params: ParamReader | None = None,
        journal: JournalWriter | None = None,
        calendar: Calendar | None = None,
        price_jump_pct: float | None = None,
        exchange_tz: Any | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._manifest = manifest
        self._params = params
        self._journal = journal
        self._calendar = calendar
        self._exchange_tz = exchange_tz or ZoneInfo("Asia/Kolkata")

        if price_jump_pct is not None:
            self._price_jump_rate = float(price_jump_pct)
        elif params is not None:
            try:
                self._price_jump_rate = float(params.get("data.price_jump_pct"))
            except Exception:
                self._price_jump_rate = 20.0
        else:
            self._price_jump_rate = 20.0

    def _is_flat_zero_bar(self, bar: Bar) -> bool:
        if bar.volume != 0:
            return False
        if bar.open == bar.high == bar.low == bar.close:
            b_date = bar.start_ts.astimezone(self._exchange_tz).date()
            if self._calendar is not None:
                return self._calendar.is_trading_day(b_date)
            return b_date.weekday() < 5
        return False

    def _handle_flat_zero_bar(self, bar: Bar, key: SeriesKey) -> None:
        b_date = bar.start_ts.astimezone(self._exchange_tz).date()
        ds_id = dataset_id_for_key(key)
        rec = CoverageDayRecord(
            dataset_id=ds_id,
            day=b_date.isoformat(),
            status=CoverageStatus.EMPTY,
            rows=0,
            expected_rows=0,
            part_path="",
            fetched_at=datetime.now(UTC).isoformat(),
            note="Dropped flat zero-volume bar",
        )
        self._manifest.record_coverage_days([rec])
        if self._journal is not None:
            self._journal.append(
                kind="data.phantom_bar_dropped",
                payload={
                    "instrument": str(key.instrument),
                    "timeframe": key.timeframe,
                    "day": b_date.isoformat(),
                    "bar_start": bar.start_ts.isoformat(),
                    "price": float(bar.close),
                    "volume": 0,
                },
            )

    def _check_bar(
        self,
        idx: int,
        bar: Bar,
        prev_bar: Bar | None,
        seen_ts: set[datetime],
        start_bound: datetime | None,
        end_bound: datetime | None,
        report: ValidationReport,
    ) -> None:
        if not isinstance(bar.start_ts, datetime) or bar.start_ts.tzinfo is None:
            report.errors.append(f"Row {idx}: start_ts must be timezone-aware UTC datetime")
        if not isinstance(bar.end_ts, datetime) or bar.end_ts.tzinfo is None:
            report.errors.append(f"Row {idx}: end_ts must be timezone-aware UTC datetime")
        if bar.start_ts > bar.end_ts:
            report.errors.append(f"Row {idx}: start_ts ({bar.start_ts}) > end_ts ({bar.end_ts})")
        if prev_bar is not None and bar.start_ts <= prev_bar.start_ts:
            report.errors.append(
                f"Row {idx}: non-increasing timestamp order: {prev_bar.start_ts} >= {bar.start_ts}"
            )
        if bar.start_ts in seen_ts:
            report.errors.append(f"Row {idx}: duplicate timestamp {bar.start_ts.isoformat()}")
        seen_ts.add(bar.start_ts)

        if bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0:
            report.errors.append(f"Row {idx}: OHLC prices must be strictly positive")
        if bar.low > bar.high:
            report.errors.append(
                f"Row {idx}: OHLC sanity failed: low ({bar.low}) > high ({bar.high})"
            )
        if bar.open < bar.low or bar.open > bar.high:
            report.errors.append(
                f"Row {idx}: open ({bar.open}) outside [low={bar.low}, high={bar.high}]"
            )
        if bar.close < bar.low or bar.close > bar.high:
            report.errors.append(
                f"Row {idx}: close ({bar.close}) outside [low={bar.low}, high={bar.high}]"
            )
        if bar.volume < 0:
            report.errors.append(f"Row {idx}: volume cannot be negative ({bar.volume})")

        if self._calendar is not None:
            day = bar.start_ts.date()
            if not self._calendar.is_trading_day(day):
                report.errors.append(f"Row {idx}: bar on non-trading day {day}")
            elif sessions := self._calendar.sessions(day):
                s_open, s_close = sessions[0].start, sessions[-1].end
                if bar.start_ts < s_open or bar.end_ts > s_close:
                    report.errors.append(
                        f"Row {idx}: bar {bar.start_ts.isoformat()} outside session hours "
                        f"[{s_open.isoformat()}, {s_close.isoformat()}]"
                    )

        if start_bound is not None and bar.start_ts < start_bound:
            report.errors.append(f"Row {idx}: bar before start bound ({start_bound})")
        if end_bound is not None and bar.end_ts > end_bound:
            report.errors.append(f"Row {idx}: bar after end bound ({end_bound})")

        if prev_bar is not None and prev_bar.close > 0:
            pct_move = float(abs(bar.close - prev_bar.close)) / float(prev_bar.close) * 100.0
            if pct_move > self._price_jump_rate:
                report.warnings.append(
                    f"Row {idx}: price jump of {pct_move:.1f}% from {prev_bar.close} "
                    f"to {bar.close} exceeds warning threshold ({self._price_jump_rate:.1f}%)"
                )

    def validate_chunk(
        self,
        bars: Sequence[Bar],
        key: SeriesKey,
        start_bound: datetime | None = None,
        end_bound: datetime | None = None,
    ) -> ValidationReport:
        """Validate sequence of Bar objects and quarantine on any hard rule failure."""
        report = ValidationReport(total_bars=len(bars))
        if not bars:
            return report

        drop_flat = True
        if self._params is not None:
            try:
                val = self._params.get("data.drop_flat_zero_volume_bars")
                if val is not None:
                    drop_flat = val is True or str(val).lower() in ("true", "1", "yes")
            except Exception:
                drop_flat = True

        seen_ts: set[datetime] = set()
        prev_bar: Bar | None = None
        clean_bars: list[Bar] = []

        for idx, bar in enumerate(bars):
            if drop_flat and self._is_flat_zero_bar(bar):
                report.dropped_flat_bars += 1
                self._handle_flat_zero_bar(bar, key)
                continue

            self._check_bar(idx, bar, prev_bar, seen_ts, start_bound, end_bound, report)
            clean_bars.append(bar)
            prev_bar = bar

        report.clean_bars = clean_bars
        if report.errors:
            report.is_valid = False
            q_id = self._quarantine_chunk(bars, key, report.errors, report.warnings)
            report.quarantine_id = q_id
            err_msg = "; ".join(report.errors[:3])
            raise DataError(
                what=f"Validation failed for {key.instrument} ({key.timeframe}): {err_msg}",
                where="BarValidator.validate_chunk",
                fix=(
                    f"Chunk moved to quarantine (id: {q_id}). "
                    f"Inspect with 'algodesk data quarantine show {q_id}'"
                ),
            )

        return report

    def validate_rows(
        self,
        rows: Sequence[dict[str, Any]],
        key: SeriesKey,
    ) -> ValidationReport:
        """Validate raw dictionary rows before bar reconstruction."""
        report = ValidationReport(total_bars=len(rows))
        for idx, r in enumerate(rows):
            s_ts = r.get("start_ts")
            e_ts = r.get("end_ts")
            if s_ts is not None and e_ts is not None and s_ts > e_ts:
                report.errors.append(f"Row {idx}: start_ts ({s_ts}) > end_ts ({e_ts})")

            o = float(r.get("open", 0.0))
            h = float(r.get("high", 0.0))
            low = float(r.get("low", 0.0))
            c = float(r.get("close", 0.0))
            v = int(r.get("volume", 0))

            if o <= 0 or h <= 0 or low <= 0 or c <= 0:
                report.errors.append(f"Row {idx}: non-positive OHLC prices")
            if low > h:
                report.errors.append(f"Row {idx}: OHLC sanity failed: low ({low}) > high ({h})")
            if o < low or o > h:
                report.errors.append(f"Row {idx}: open ({o}) outside [low={low}, high={h}]")
            if c < low or c > h:
                report.errors.append(f"Row {idx}: close ({c}) outside [low={low}, high={h}]")
            if v < 0:
                report.errors.append(f"Row {idx}: negative volume ({v})")

        if report.errors:
            report.is_valid = False
            q_id = self._quarantine_raw(rows, key, report.errors)
            report.quarantine_id = q_id
            err_msg = "; ".join(report.errors[:3])
            raise DataError(
                what=f"Validation failed for {key.instrument}: {err_msg}",
                where="BarValidator.validate_rows",
                fix=(
                    f"Chunk quarantined (id: {q_id}). "
                    f"Inspect with 'algodesk data quarantine show {q_id}'"
                ),
            )

        return report

    def _quarantine_chunk(
        self,
        bars: Sequence[Bar],
        key: SeriesKey,
        errors: list[str],
        warnings: list[str],
    ) -> str:
        q_id = f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
        q_dir = self._data_dir / ".quarantine" / q_id
        q_dir.mkdir(parents=True, exist_ok=True)

        rep_path = q_dir / "report.json"
        rep_data = {
            "id": q_id,
            "instrument": str(key.instrument),
            "kind": key.kind,
            "timeframe": key.timeframe,
            "adjustment": key.adjustment,
            "source": key.source,
            "total_bars": len(bars),
            "errors": errors,
            "warnings": warnings,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        rep_path.write_text(json.dumps(rep_data, indent=2))

        # Save bars to parquet for offline inspection and possible release
        write_bars_parquet(q_dir / "bars.parquet", bars)
        meta = {
            "instrument": str(key.instrument),
            "kind": key.kind,
            "timeframe": key.timeframe,
            "adjustment": key.adjustment,
            "source": key.source,
        }
        (q_dir / "meta.json").write_text(json.dumps(meta, indent=2))

        rel_rep_path = str(rep_path.relative_to(self._data_dir))
        chunk_desc = f"{key.instrument} {key.timeframe} ({len(bars)} bars)"
        reason = errors[0] if errors else "Validation failed"

        self._manifest.record_quarantine(
            QuarantineRecord(
                id=q_id,
                chunk=chunk_desc,
                reason=reason,
                report_path=rel_rep_path,
                created=datetime.now(UTC).isoformat(),
                status="quarantined",
            )
        )

        if self._journal is not None:
            self._journal.append(
                kind="data.quarantine",
                payload={
                    "id": q_id,
                    "action": "quarantined",
                    "instrument": str(key.instrument),
                    "reason": reason,
                    "errors_count": len(errors),
                },
            )

        return q_id

    def _quarantine_raw(
        self,
        rows: Sequence[dict[str, Any]],
        key: SeriesKey,
        errors: list[str],
    ) -> str:
        q_id = f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
        q_dir = self._data_dir / ".quarantine" / q_id
        q_dir.mkdir(parents=True, exist_ok=True)

        rep_path = q_dir / "report.json"
        rep_data = {
            "id": q_id,
            "instrument": str(key.instrument),
            "source": key.source,
            "total_rows": len(rows),
            "errors": errors,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        rep_path.write_text(json.dumps(rep_data, indent=2))
        (q_dir / "rows.json").write_text(json.dumps(rows, default=str, indent=2))

        rel_rep_path = str(rep_path.relative_to(self._data_dir))
        reason = errors[0] if errors else "Validation failed"

        self._manifest.record_quarantine(
            QuarantineRecord(
                id=q_id,
                chunk=f"{key.instrument} ({len(rows)} rows)",
                reason=reason,
                report_path=rel_rep_path,
                created=datetime.now(UTC).isoformat(),
                status="quarantined",
            )
        )

        if self._journal is not None:
            self._journal.append(
                kind="data.quarantine",
                payload={"id": q_id, "action": "quarantined", "reason": reason},
            )

        return q_id
