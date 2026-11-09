"""Vendor market data importer for AlgoDesk Next.

Reads CSV or Parquet bar/tick files with column mappings, maps symbols through
structured instrument identity, passes data through the standard validator and BarStore,
and uses the file SHA-256 checksum as the source version.
"""

from __future__ import annotations

import csv
import hashlib
import zoneinfo
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from algodesk.api.alias import resolve_alias
from algodesk.api.errors import ConfigError, DataError
from algodesk.api.market import Bar, InstrumentId, SeriesKey, Tick, parse_id
from algodesk.data.schema import events_to_table

if TYPE_CHECKING:
    from algodesk.data.store import BarStore
    from algodesk.data.validator import BarValidator


@dataclass(frozen=True)
class ImportResult:
    """Summary of imported vendor data."""

    path: str
    sha256: str
    kind: str
    timeframe: str
    rows_count: int
    instruments: list[str]
    first_ts: str
    last_ts: str
    dry_run: bool = False


def compute_file_sha256(path: Path | str) -> str:
    """Compute hex SHA-256 checksum of file."""
    p = Path(path)
    if not p.is_file():
        raise DataError(
            what=f"File not found: {p}",
            where="importer.sha256",
            fix="Check file path exists and is readable",
        )
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def load_mapping_file(mapping_path: Path | str) -> dict[str, Any]:
    """Load column mapping configuration from TOML or JSON file."""
    p = Path(mapping_path)
    if not p.is_file():
        raise ConfigError(
            what=f"Mapping file not found at {p}",
            where="importer.mapping",
            fix="Provide valid path to mapping TOML or JSON file",
        )
    content = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        import json

        return cast(dict[str, Any], json.loads(content))
    try:
        import tomllib

        return tomllib.loads(content)
    except Exception as exc:
        raise ConfigError(
            what=f"Failed to parse mapping file {p}: {exc}",
            where="importer.mapping",
            fix="Verify mapping file is valid TOML or JSON",
        ) from exc


def _get_data_dir(runtime: Any) -> Path:
    """Resolve data directory from runtime or path."""
    if isinstance(runtime, Path):
        return runtime / "data" if (runtime / "data").exists() else runtime
    return Path(runtime.paths.data) if hasattr(runtime, "paths") else Path(runtime.root) / "data"


def _resolve_symbol(raw_sym: str, default_sym: str | None = None) -> InstrumentId:
    """Resolve raw symbol to canonical structured InstrumentId."""
    sym = (raw_sym or default_sym or "").strip()
    if not sym:
        raise DataError(
            what="No symbol specified in mapping or vendor row",
            where="importer.symbol",
            fix="Provide 'symbol' in mapping file or mapping.columns.symbol",
        )
    if ":" in sym:
        parse_id(sym)
        return InstrumentId(sym.upper())
    alias = str(resolve_alias(sym))
    if ":" in alias:
        return InstrumentId(alias)
    return InstrumentId(f"NSE:EQ:{sym.upper()}")


def _parse_ts(raw_ts: Any, tz_name: str) -> datetime:
    """Parse timestamp into timezone-aware UTC datetime."""
    if isinstance(raw_ts, datetime):
        dt = raw_ts
    elif isinstance(raw_ts, str):
        # Handle ISO format or common datetime strings
        cleaned = raw_ts.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(cleaned)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
                try:
                    dt = datetime.strptime(cleaned, fmt)
                    break
                except ValueError:
                    pass
            else:
                raise ValueError(f"Unrecognized timestamp format: {raw_ts}") from None
    elif isinstance(raw_ts, (int, float)):
        # Epoch seconds or milliseconds
        val = float(raw_ts)
        if val > 1e11:  # ms
            val /= 1000.0
        dt = datetime.fromtimestamp(val, tz=UTC)
    else:
        raise ValueError(f"Unsupported timestamp type: {type(raw_ts)}")

    if dt.tzinfo is None:
        source_tz = zoneinfo.ZoneInfo(tz_name)
        dt = dt.replace(tzinfo=source_tz)
    return dt.astimezone(UTC)


def _read_vendor_rows(path: Path) -> list[dict[str, Any]]:
    """Read CSV or Parquet file into list of row dictionaries."""
    if path.suffix.lower() == ".parquet":
        import pyarrow.parquet as pq

        tbl = pq.read_table(path)
        return cast(list[dict[str, Any]], tbl.to_pylist())

    # CSV reader
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return cast(list[dict[str, Any]], list(reader))


def _build_bars(
    rows: list[dict[str, Any]],
    mapping: dict[str, Any],
    tz_name: str,
) -> list[Bar]:
    """Parse raw rows into structured Bar instances."""
    cols = mapping.get("columns", {})
    ts_col = cols.get("timestamp", "timestamp")
    o_col = cols.get("open", "open")
    h_col = cols.get("high", "high")
    l_col = cols.get("low", "low")
    c_col = cols.get("close", "close")
    v_col = cols.get("volume", "volume")
    s_col = cols.get("symbol")
    def_sym = mapping.get("symbol")
    tf = mapping.get("timeframe", "1m")

    bars: list[Bar] = []
    for r in rows:
        sym_str = str(r.get(s_col) if s_col else def_sym or "")
        inst_id = _resolve_symbol(sym_str, def_sym)
        ts = _parse_ts(r[ts_col], tz_name)
        delta = timedelta(minutes=1) if tf == "1m" else timedelta(days=1)
        b = Bar(
            instrument=inst_id,
            timeframe=tf,
            start_ts=ts,
            end_ts=ts + delta,
            open=float(r[o_col]),
            high=float(r[h_col]),
            low=float(r[l_col]),
            close=float(r[c_col]),
            volume=int(float(r.get(v_col, 0) or 0)),
        )
        bars.append(b)
    bars.sort(key=lambda x: x.start_ts)
    return bars


def _build_ticks(
    rows: list[dict[str, Any]],
    mapping: dict[str, Any],
    tz_name: str,
) -> list[Tick]:
    """Parse raw rows into structured Tick instances."""
    cols = mapping.get("columns", {})
    ts_col = cols.get("timestamp", "timestamp")
    p_col = cols.get("price", "price")
    v_col = cols.get("volume", "volume")
    s_col = cols.get("symbol")
    def_sym = mapping.get("symbol")

    ticks: list[Tick] = []
    for r in rows:
        sym_str = str(r.get(s_col) if s_col else def_sym or "")
        inst_id = _resolve_symbol(sym_str, def_sym)
        ts = _parse_ts(r[ts_col], tz_name)
        t = Tick(
            instrument=inst_id,
            exch_ts=ts,
            recv_ts=ts,
            ltp=float(r[p_col]),
            ltq=int(float(r.get("qty", 1) or 1)),
            volume=int(float(r.get(v_col, 0) or 0)),
        )
        ticks.append(t)
    ticks.sort(key=lambda x: x.exch_ts)
    return ticks


def _write_single_tick_partition(
    p_dir: Path,
    inst_str: str,
    d_str: str,
    inst_ticks: list[Tick],
    sha256: str,
    manifest: Any,
) -> None:
    """Write partition parquet and record in manifest."""
    import pyarrow.parquet as pq

    from algodesk.data.manifest import FileRecord

    p_dir.mkdir(parents=True, exist_ok=True)
    f_path = p_dir / "ticks.parquet"
    tbl = events_to_table(inst_ticks)
    pq.write_table(tbl, f_path, compression="zstd")

    rec = FileRecord(
        path=str(f_path),
        instrument=inst_str,
        kind="ticks",
        timeframe="tick",
        date=d_str,
        rows=len(inst_ticks),
        bytes=f_path.stat().st_size,
        first_ts=inst_ticks[0].exch_ts.isoformat(),
        last_ts=inst_ticks[-1].exch_ts.isoformat(),
        sha256=sha256,
        source="importer",
        state="tick",
        created_ts=datetime.now(UTC).isoformat(),
    )
    manifest.record_file(rec)


def _store_imported_ticks(
    ticks: list[Tick],
    runtime: Any,
    sha256: str,
) -> None:
    """Write imported ticks into partition directory and register in manifest."""
    d_dir = _get_data_dir(runtime)
    manifest_db = d_dir / "state" / "data_manifest.db"
    if not manifest_db.exists() and (d_dir / "manifest.db").exists():
        manifest_db = d_dir / "manifest.db"
    if not manifest_db.exists():
        return

    from algodesk.data.manifest import DataManifest

    m = DataManifest(manifest_db)
    try:
        by_inst_date: dict[tuple[str, str], list[Tick]] = {}
        for t in ticks:
            d_str = t.exch_ts.date().isoformat()
            by_inst_date.setdefault((str(t.instrument), d_str), []).append(t)

        for (inst_str, d_str), inst_ticks in by_inst_date.items():
            parts = parse_id(inst_str)
            p_dir = (
                d_dir
                / "ticks"
                / f"exchange={parts.exchange}"
                / f"segment={parts.segment}"
                / f"symbol={parts.symbol}"
                / f"date={d_str}"
            )
            _write_single_tick_partition(p_dir, inst_str, d_str, inst_ticks, sha256, m)
    finally:
        m.close()


def _handle_bars_import(
    bars: list[Bar],
    timeframe: str,
    sha256: str,
    validator: BarValidator | None,
    store: BarStore | None,
    dry_run: bool,
) -> None:
    """Validate and commit imported bars to BarStore."""
    if validator is not None and bars:
        key = SeriesKey(
            instrument=bars[0].instrument,
            kind="bars",
            timeframe=timeframe,
            adjustment="raw",
            source="importer",
        )
        rep = validator.validate_chunk(bars, key)
        if not rep.is_valid:
            err_msg = "; ".join(rep.errors[:5])
            raise DataError(
                what=f"Validator rejected imported bars: {err_msg}",
                where="data.import",
                fix="Clean invalid rows or configure price jump tolerances",
            )

    if not dry_run and store is not None:
        by_inst: dict[InstrumentId, list[Bar]] = {}
        for b in bars:
            by_inst.setdefault(b.instrument, []).append(b)
        for inst, inst_bars in by_inst.items():
            k = SeriesKey(
                instrument=inst,
                kind="bars",
                timeframe=timeframe,
                adjustment="raw",
                source="importer",
            )
            store.commit(k, inst_bars, refresh=False, source_version=sha256, validator=validator)


def _process_vendor_import(
    kind: str,
    rows: list[dict[str, Any]],
    mapping: dict[str, Any],
    tz_name: str,
    timeframe: str,
    sha256: str,
    validator: BarValidator | None,
    store: BarStore | None,
    runtime: Any,
    dry_run: bool,
) -> tuple[list[str], str, str]:
    """Process import rows according to kind ('bars' or 'ticks')."""
    if kind == "bars":
        bars = _build_bars(rows, mapping, tz_name)
        insts = sorted(list({str(b.instrument) for b in bars}))
        _handle_bars_import(bars, timeframe, sha256, validator, store, dry_run)
        return insts, bars[0].start_ts.isoformat(), bars[-1].start_ts.isoformat()
    if kind == "ticks":
        ticks = _build_ticks(rows, mapping, tz_name)
        insts = sorted(list({str(t.instrument) for t in ticks}))
        if not dry_run:
            _store_imported_ticks(ticks, runtime, sha256)
        return insts, ticks[0].exch_ts.isoformat(), ticks[-1].exch_ts.isoformat()
    raise DataError(
        what=f"Unsupported import kind '{kind}'. Expected 'bars' or 'ticks'",
        where="data.import",
        fix="Specify kind = 'bars' or kind = 'ticks' in mapping file",
    )


def _journal_import(
    runtime: Any,
    f_path: Path,
    sha256: str,
    kind: str,
    timeframe: str,
    rows_count: int,
) -> None:
    if hasattr(runtime, "journal") and runtime.journal is not None:
        runtime.journal.append(
            "data.import",
            {
                "file": str(f_path),
                "sha256": sha256,
                "kind": kind,
                "timeframe": timeframe,
                "rows": rows_count,
            },
            durable=True,
        )


def import_vendor_file(
    file_path: Path | str,
    mapping_path: Path | str,
    runtime: Any,
    store: BarStore | None = None,
    validator: BarValidator | None = None,
    dry_run: bool = False,
) -> ImportResult:
    """Import CSV or Parquet file using mapping file into data store."""
    f_path = Path(file_path)
    sha256 = compute_file_sha256(f_path)
    mapping = load_mapping_file(mapping_path)
    kind = str(mapping.get("kind", "bars")).lower()
    timeframe = str(mapping.get("timeframe", "1m" if kind == "bars" else "tick")).lower()
    tz_name = str(mapping.get("timezone", "Asia/Kolkata"))

    rows = _read_vendor_rows(f_path)
    if not rows:
        raise DataError(
            what=f"Vendor file at {f_path} contains zero rows",
            where="data.import",
            fix="Provide non-empty data file",
        )

    instruments, first_ts, last_ts = _process_vendor_import(
        kind, rows, mapping, tz_name, timeframe, sha256, validator, store, runtime, dry_run
    )

    if not dry_run:
        _journal_import(runtime, f_path, sha256, kind, timeframe, len(rows))

    return ImportResult(
        path=str(f_path),
        sha256=sha256,
        kind=kind,
        timeframe=timeframe,
        rows_count=len(rows),
        instruments=instruments,
        first_ts=first_ts,
        last_ts=last_ts,
        dry_run=dry_run,
    )
