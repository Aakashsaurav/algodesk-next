"""Instrument groups loader from config/groups/*.toml and data/universe/*.parquet.

Enables grouping instruments by thematic collections (e.g. demo5, banks, indices)
for universe selection, data querying, and bulk deletion.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from algodesk.api.errors import ConfigError, DataError


def _resolve_groups_dir(groups_dir: Path | str | None) -> Path:
    if groups_dir is not None:
        p = Path(groups_dir)
        return p if p.is_absolute() else Path.cwd() / p
    return Path.cwd() / "config" / "groups"


def _resolve_universe_dir(universe_dir: Path | str | None) -> Path:
    if universe_dir is not None:
        p = Path(universe_dir)
        return p if p.is_absolute() else Path.cwd() / p
    return Path.cwd() / "data" / "universe"


def load_groups(groups_dir: Path | str | None = None) -> dict[str, list[str]]:
    """Load all instrument groups returning a mapping of name to member list."""
    p = _resolve_groups_dir(groups_dir)
    if not p.is_dir():
        return {}

    groups: dict[str, list[str]] = {}
    for toml_path in sorted(p.glob("*.toml")):
        if toml_path.name.endswith(".unmapped.toml") or toml_path.name.startswith("."):
            continue
        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
            name = str(data.get("name", toml_path.stem))
            members_raw = data.get("members", [])
            members = [str(m).strip() for m in members_raw if str(m).strip()]
            groups[name] = members
        except Exception as exc:
            msg = f"Failed to load group TOML file: {exc}"
            raise ConfigError(
                what=msg, where=str(toml_path), fix="Verify valid TOML syntax"
            ) from exc

    return groups


def list_group_details(groups_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Load full group metadata including name, description, and members."""
    p = _resolve_groups_dir(groups_dir)
    if not p.is_dir():
        return {}

    details: dict[str, dict[str, Any]] = {}
    for toml_path in sorted(p.glob("*.toml")):
        if toml_path.name.endswith(".unmapped.toml") or toml_path.name.startswith("."):
            continue
        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
            name = str(data.get("name", toml_path.stem))
            desc = str(data.get("description", ""))
            members_raw = data.get("members", [])
            members = [str(m).strip() for m in members_raw if str(m).strip()]
            details[name] = {
                "name": name,
                "description": desc,
                "members": members,
                "file": toml_path.name,
            }
        except Exception as exc:
            msg = f"Failed to load group details: {exc}"
            raise ConfigError(
                what=msg, where=str(toml_path), fix="Verify valid TOML syntax"
            ) from exc

    return details


def get_group(name: str, groups_dir: Path | str | None = None) -> list[str] | None:
    """Retrieve members for a single group by name, or None if not found."""
    groups = load_groups(groups_dir)
    return groups.get(name)


def _load_instrument_indices(
    data_dir: Path | None = None,
    source: str = "upstox",
) -> tuple[dict[str, str], dict[str, str]]:
    """Build (isin_to_id, sym_to_id) lookup tables from master snapshots or cache.

    Returns:
        (isin_map, sym_map) where values are canonical InstrumentIds (e.g. 'NSE:EQ:RELIANCE').
    """
    isin_map: dict[str, str] = {}
    sym_map: dict[str, str] = {}

    # 1. Try InstrumentStore in data_dir
    if data_dir is not None and (data_dir / "instruments" / f"source={source}").is_dir():
        try:
            from algodesk.data.instruments_store import InstrumentStore

            store = InstrumentStore(data_dir)
            dates = store.list_dates(source)
            if dates:
                insts = store.load_master(source, dates[-1])
                for inst in insts:
                    if inst.exchange == "NSE" and inst.segment == "EQ":
                        cid = str(inst.id)
                        if inst.isin:
                            isin_map[inst.isin] = cid
                        if inst.trading_symbol:
                            sym_map[inst.trading_symbol.upper()] = cid
                        if inst.symbol:
                            sym_map[inst.symbol.upper()] = cid
                if isin_map or sym_map:
                    return isin_map, sym_map
        except Exception:
            pass

    # 2. Try cache directories
    cache_dirs: list[Path] = [
        Path(".local/cache/instruments"),
        Path("local/cache/instruments"),
        Path("data/instruments/source=upstox"),
    ]
    if data_dir is not None:
        cache_dirs.append(data_dir / "instruments" / f"source={source}")

    for c_dir in cache_dirs:
        if c_dir.is_dir():
            gz_files = sorted(c_dir.glob("*.json.gz")) + sorted(c_dir.glob("*/*.json.gz"))
            if gz_files:
                try:
                    content = gz_files[-1].read_bytes()
                    raw = json.loads(gzip.decompress(content))
                    for item in raw:
                        exch = str(item.get("exchange") or "NSE").upper()
                        seg = str(item.get("segment") or "EQ").upper()
                        if seg == "NSE_EQ" or (exch == "NSE" and seg in ("EQ", "")):
                            raw_tsym = (
                                item.get("trading_symbol")
                                or item.get("tradingsymbol")
                                or item.get("symbol")
                                or ""
                            )
                            tsym = str(raw_tsym).strip().upper()
                            isin = str(item.get("isin") or "").strip()
                            cid = f"NSE:EQ:{tsym}"
                            if isin and isin not in isin_map:
                                isin_map[isin] = cid
                            if tsym and tsym not in sym_map:
                                sym_map[tsym] = cid
                    if isin_map or sym_map:
                        return isin_map, sym_map
                except Exception:
                    pass

    return isin_map, sym_map


@dataclass
class GroupImportResult:
    """Result of importing a current index CSV into a group."""

    group_name: str
    source_file: str
    source_sha256: str
    total_rows: int
    mapped_count: int
    unmapped_count: int
    unmapped_pct: float
    mapped_members: list[str]
    unmapped_members: list[dict[str, Any]]
    group_file: Path
    sidecar_file: Path


def import_group(
    csv_path: Path | str,
    group_name: str,
    groups_dir: Path | str | None = None,
    accept_unmapped: bool = False,
    data_dir: Path | str | None = None,
    params: Any = None,
    journal: Any = None,
    description: str | None = None,
) -> GroupImportResult:
    """Import an NSE current index CSV into config/groups/<group_name>.toml."""
    csv_p = Path(csv_path)
    if not csv_p.is_file():
        raise DataError(
            what=f"Source CSV file '{csv_p}' not found",
            where="groups.import",
            fix="Check file path and existence",
        )

    raw_bytes = csv_p.read_bytes()
    source_sha = hashlib.sha256(raw_bytes).hexdigest()

    text = raw_bytes.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise DataError(
            what=f"Empty CSV file '{csv_p}'",
            where="groups.import",
            fix="Ensure CSV file contains headers and rows",
        )

    norm_fields = {f.strip().lower(): f for f in reader.fieldnames}
    sym_col = norm_fields.get("symbol")
    isin_col = norm_fields.get("isin code")
    if not sym_col or not isin_col:
        raise DataError(
            what=(
                f"Unrecognized CSV format in '{csv_p}'. Expected NSE current columns: "
                f"'Symbol' and 'ISIN Code' (found: {', '.join(reader.fieldnames)})"
            ),
            where="groups.import",
            fix=(
                "Ensure CSV is in NSE current index format "
                "(Company Name, Industry, Symbol, Series, ISIN Code)"
            ),
        )

    rows = list(reader)
    if not rows:
        raise DataError(
            what=f"No data rows found in '{csv_p}'",
            where="groups.import",
            fix="Verify CSV file contains constituent data",
        )

    resolved_data_dir = Path(data_dir) if data_dir else Path.cwd() / "data"
    isin_map, sym_map = _load_instrument_indices(data_dir=resolved_data_dir)

    mapped_members: list[str] = []
    unmapped_members: list[dict[str, Any]] = []

    for row in rows:
        sym = str(row.get(sym_col, "")).strip().upper()
        isin = str(row.get(isin_col, "")).strip()

        target_cid: str | None = None
        if isin and isin in isin_map:
            target_cid = isin_map[isin]
        elif sym and sym in sym_map:
            target_cid = sym_map[sym]

        if target_cid:
            mapped_members.append(target_cid)
        else:
            unmapped_members.append(
                {
                    "symbol": sym,
                    "isin": isin,
                    "series": row.get(norm_fields.get("Series", "Series"), "").strip(),
                    "company_name": row.get(
                        norm_fields.get("Company Name", "Company Name"), ""
                    ).strip(),
                }
            )

    total_rows = len(rows)
    unmapped_count = len(unmapped_members)
    mapped_count = len(mapped_members)
    unmapped_pct = (unmapped_count / total_rows * 100.0) if total_rows > 0 else 0.0

    allowed_unmapped_pct: float | None = None
    if params is not None:
        try:
            val = params.get("groups.import_max_unmapped_pct")
            if val is not None:
                allowed_unmapped_pct = float(val)
        except Exception:
            pass
    if allowed_unmapped_pct is None:
        allowed_unmapped_pct = float(5)

    if unmapped_pct > allowed_unmapped_pct and not accept_unmapped:
        raise DataError(
            what=(
                f"Unmapped instruments ({unmapped_pct:.1f}%) exceeds maximum allowed "
                f"{allowed_unmapped_pct}% ({unmapped_count}/{total_rows} unmapped)"
            ),
            where="groups.import",
            fix="Pass --accept-unmapped to proceed anyway or update instrument master",
        )

    p_dir = _resolve_groups_dir(groups_dir)
    p_dir.mkdir(parents=True, exist_ok=True)

    group_file = p_dir / f"{group_name}.toml"
    sidecar_file = p_dir / f"{group_name}.unmapped.json"

    desc = description or f"Imported from {csv_p.name}"
    members_block = "\n".join(f'    "{m}",' for m in mapped_members)
    toml_content = (
        f'name = "{group_name}"\ndescription = "{desc}"\nmembers = [\n{members_block}\n]\n'
    )
    group_file.write_text(toml_content, encoding="utf-8")

    sidecar_payload = {
        "group": group_name,
        "source_file": csv_p.name,
        "source_sha256": source_sha,
        "total_rows": total_rows,
        "mapped_count": mapped_count,
        "unmapped_count": unmapped_count,
        "unmapped_pct": round(unmapped_pct, 2),
        "unmapped": unmapped_members,
    }
    sidecar_file.write_text(json.dumps(sidecar_payload, indent=2), encoding="utf-8")

    if journal is not None:
        try:
            journal.append(
                kind="groups.import",
                payload={
                    "group": group_name,
                    "sha256": source_sha,
                    "total": total_rows,
                    "mapped": mapped_count,
                    "unmapped": unmapped_count,
                },
                durable=True,
            )
        except Exception:
            pass

    return GroupImportResult(
        group_name=group_name,
        source_file=csv_p.name,
        source_sha256=source_sha,
        total_rows=total_rows,
        mapped_count=mapped_count,
        unmapped_count=unmapped_count,
        unmapped_pct=unmapped_pct,
        mapped_members=mapped_members,
        unmapped_members=unmapped_members,
        group_file=group_file,
        sidecar_file=sidecar_file,
    )


@dataclass
class HistoryImportResult:
    """Result of importing index constituent history into data/universe/."""

    name: str
    index_type: str
    source_file: str
    source_sha256: str
    total_rows: int
    intervals_count: int
    inconsistencies: list[dict[str, Any]]
    parquet_file: Path
    comparison: dict[str, Any] | None = None
    sidecar_file: Path | None = None


def import_history_group(
    csv_path: Path | str,
    name: str,
    index_type: str,
    universe_dir: Path | str | None = None,
    groups_dir: Path | str | None = None,
    accept_inconsistent: bool = False,
    data_dir: Path | str | None = None,
    params: Any = None,
    journal: Any = None,
    strict: bool = False,
) -> HistoryImportResult:
    """Import index constituent history CSV into Parquet intervals."""
    csv_p = Path(csv_path)
    if not csv_p.is_file():
        raise DataError(
            what=f"Source history CSV file '{csv_p}' not found",
            where="groups.import-history",
            fix="Check file path and existence",
        )

    raw_bytes = csv_p.read_bytes()
    source_sha = hashlib.sha256(raw_bytes).hexdigest()

    text = raw_bytes.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise DataError(
            what=f"Empty CSV file '{csv_p}'",
            where="groups.import-history",
            fix="Ensure CSV file contains headers and rows",
        )

    raw_rows = list(reader)
    # Filter by index_type
    filtered_rows: list[tuple[int, dict[str, str]]] = []
    all_index_types: set[str] = set()

    for i, r in enumerate(raw_rows, start=2):
        itype = str(r.get("index_type", "")).strip()
        if itype:
            all_index_types.add(itype)
        if itype.lower() == index_type.strip().lower():
            filtered_rows.append((i, r))

    if not filtered_rows:
        available = sorted(list(all_index_types)) or ["none"]
        raise DataError(
            what=f"No rows matching index_type '{index_type}' in {csv_p.name}",
            where="groups.import-history",
            fix=f"Available index types: {', '.join(available)}",
        )

    # Validate ISO dates and ordering strictly
    parsed_rows: list[tuple[int, date, list[str], list[str], list[str]]] = []
    prev_d: date | None = None
    for line_num, r in filtered_rows:
        raw_d = str(r.get("effective_date", "")).strip()
        try:
            d_val = date.fromisoformat(raw_d)
        except Exception as err:
            raise DataError(
                what=f"Invalid non-ISO date '{raw_d}' at line {line_num}",
                where="groups.import-history",
                fix="Ensure effective_date is in ISO format YYYY-MM-DD",
            ) from err

        if prev_d is not None:
            if d_val == prev_d:
                raise DataError(
                    what=f"Repeated effective date '{d_val}' at line {line_num}",
                    where="groups.import-history",
                    fix="Ensure effective dates are strictly increasing without duplicates",
                )
            if d_val < prev_d:
                raise DataError(
                    what=(
                        f"Decreasing effective date '{d_val}' at line {line_num} "
                        f"(previous: {prev_d})"
                    ),
                    where="groups.import-history",
                    fix="Ensure rows are ordered chronologically by effective date",
                )
        prev_d = d_val

        sym_str = str(r.get("symbols", "")).strip()
        inc_str = str(r.get("inclusions", "")).strip()
        exc_str = str(r.get("exclusions", "")).strip()

        syms = [s.strip().upper() for s in sym_str.split(",") if s.strip()] if sym_str else []
        incs = [s.strip().upper() for s in inc_str.split(",") if s.strip()] if inc_str else []
        excs = [s.strip().upper() for s in exc_str.split(",") if s.strip()] if exc_str else []

        if not syms:
            raise DataError(
                what=f"Empty snapshot on {d_val} at line {line_num}",
                where="groups.import-history",
                fix="Every effective date must contain at least one constituent symbol",
            )

        parsed_rows.append((line_num, d_val, syms, incs, excs))

    # Validate size change between consecutive dates against parameter registry
    allowed_delta: int | None = None
    if params is not None:
        try:
            val = params.get("groups.history_max_size_jump")
            if val is not None:
                allowed_delta = int(val)
        except Exception:
            pass
    if allowed_delta is None:
        allowed_delta = 100

    prev_sym_count: int | None = None
    for line_num, d_val, syms, _incs, _excs in parsed_rows:
        if prev_sym_count is not None:
            jump = abs(len(syms) - prev_sym_count)
            if jump > allowed_delta:
                raise DataError(
                    what=(
                        f"Size change of {jump} symbols on {d_val} at line {line_num} "
                        f"exceeds maximum allowed jump of {allowed_delta} "
                        "(groups.history_max_size_jump)"
                    ),
                    where="groups.import-history",
                    fix="Verify data integrity or adjust groups.history_max_size_jump in params",
                )
        prev_sym_count = len(syms)

    # Check list inconsistencies: symbols column is authority,
    # inclusions/exclusions are cross-checks
    inconsistencies: list[dict[str, Any]] = []
    prev_set: set[str] | None = None

    for line_num, d_val, syms, incs, excs in parsed_rows:
        curr_set = set(syms)
        if prev_set is None:
            prev_set = curr_set
        else:
            expected_set = (prev_set | set(incs)) - set(excs)
            if curr_set != expected_set:
                inconsistencies.append(
                    {
                        "line": line_num,
                        "date": d_val.isoformat(),
                        "expected_count": len(expected_set),
                        "actual_count": len(curr_set),
                        "missing": sorted(expected_set - curr_set),
                        "extra": sorted(curr_set - expected_set),
                    }
                )
            prev_set = curr_set

    # Write sidecar JSON with mismatches
    u_dir = _resolve_universe_dir(universe_dir)
    u_dir.mkdir(parents=True, exist_ok=True)
    sidecar_p = u_dir / f"{name}.inconsistencies.json"
    sidecar_data = {
        "name": name,
        "index_type": index_type,
        "source_file": csv_p.name,
        "inconsistencies_count": len(inconsistencies),
        "inconsistencies": inconsistencies,
    }
    sidecar_p.write_text(json.dumps(sidecar_data, indent=2), encoding="utf-8")

    if inconsistencies and strict:
        raise DataError(
            what=(
                f"Found {len(inconsistencies)} list inconsistencies in history "
                f"constituents for {index_type}"
            ),
            where="groups.import-history",
            fix="Run without --strict to import with warnings and sidecar JSON",
        )

    # Build membership intervals
    active_intervals: dict[str, str] = {}
    completed_intervals: list[tuple[str, str, str | None]] = []

    for _line, d_val, syms, _incs, _excs in parsed_rows:
        curr_syms_set = set(syms)
        curr_d_str = d_val.isoformat()

        # Removed symbols
        removed = set(active_intervals.keys()) - curr_syms_set
        for sym in sorted(removed):
            start_d = active_intervals.pop(sym)
            completed_intervals.append((sym, start_d, curr_d_str))

        # Added symbols
        added = curr_syms_set - set(active_intervals.keys())
        for sym in sorted(added):
            active_intervals[sym] = curr_d_str

    # Terminal active symbols have to = None
    for sym, start_d in sorted(active_intervals.items()):
        completed_intervals.append((sym, start_d, None))

    # Load instrument indices to map canonical IDs
    resolved_data_dir = Path(data_dir) if data_dir else Path.cwd() / "data"
    _, sym_map = _load_instrument_indices(data_dir=resolved_data_dir)

    symbols_col: list[str] = []
    from_col: list[str] = []
    to_col: list[str | None] = []
    canonical_id_col: list[str | None] = []

    for sym, f_date, t_date in completed_intervals:
        symbols_col.append(sym)
        from_col.append(f_date)
        to_col.append(t_date)
        cid = sym_map.get(sym) or sym_map.get(sym.upper())
        if not cid and f"NSE:EQ:{sym}" in sym_map.values():
            cid = f"NSE:EQ:{sym}"
        canonical_id_col.append(cid)

    u_dir = _resolve_universe_dir(universe_dir)
    u_dir.mkdir(parents=True, exist_ok=True)
    parquet_p = u_dir / f"{name}.parquet"

    tbl = pa.Table.from_arrays(
        [
            pa.array(symbols_col, type=pa.string()),
            pa.array(from_col, type=pa.string()),
            pa.array(to_col, type=pa.string()),
            pa.array(canonical_id_col, type=pa.string()),
        ],
        names=["symbol", "from", "to", "canonical_id"],
    )

    metadata = {
        b"source_sha256": source_sha.encode("utf-8"),
        b"name": name.encode("utf-8"),
        b"index_type": index_type.encode("utf-8"),
        b"rows_count": str(len(completed_intervals)).encode("utf-8"),
    }
    tbl = tbl.replace_schema_metadata(metadata)
    pq.write_table(tbl, parquet_p)

    # Compare last set with current group if one exists
    comp_info: dict[str, Any] | None = None
    cand_names = [name.replace("_history", ""), name]
    groups_p = _resolve_groups_dir(groups_dir)
    for cand in cand_names:
        cg_file = groups_p / f"{cand}.toml"
        if cg_file.is_file():
            try:
                with open(cg_file, "rb") as f:
                    cg_data = tomllib.load(f)
                c_members = cg_data.get("members", [])
                c_syms = set()
                for m in c_members:
                    c_syms.add(str(m).split(":")[-1].upper())

                last_syms = set(parsed_rows[-1][2])
                in_h_not_c = sorted(last_syms - c_syms)
                in_c_not_h = sorted(c_syms - last_syms)
                comp_info = {
                    "current_group": cand,
                    "last_history_date": parsed_rows[-1][1].isoformat(),
                    "history_last_count": len(last_syms),
                    "current_group_count": len(c_syms),
                    "in_history_not_current": in_h_not_c,
                    "in_current_not_history": in_c_not_h,
                }
                break
            except Exception:
                pass

    if journal is not None:
        try:
            journal.append(
                kind="groups.import-history",
                payload={
                    "name": name,
                    "index": index_type,
                    "sha256": source_sha,
                    "intervals": len(completed_intervals),
                },
                durable=True,
            )
        except Exception:
            pass

    return HistoryImportResult(
        name=name,
        index_type=index_type,
        source_file=csv_p.name,
        source_sha256=source_sha,
        total_rows=len(filtered_rows),
        intervals_count=len(completed_intervals),
        inconsistencies=inconsistencies,
        parquet_file=parquet_p,
        comparison=comp_info,
        sidecar_file=sidecar_p,
    )


def resolve_history_group(
    name: str,
    as_of: str | date,
    universe_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Resolve members of a history group valid on a given as-of date."""
    u_dir = _resolve_universe_dir(universe_dir)
    parquet_p = u_dir / f"{name}.parquet"
    if not parquet_p.is_file():
        raise ConfigError(
            what=f"History group universe file '{parquet_p}' not found",
            where="groups.show",
            fix="Import history group first via 'algodesk groups import-history'",
        )

    as_of_str = as_of.isoformat() if isinstance(as_of, date) else str(as_of).strip()
    try:
        date.fromisoformat(as_of_str)
    except Exception as e:
        raise ConfigError(
            what=f"Invalid as-of date format '{as_of_str}'",
            where="groups.show",
            fix="Specify as-of date in ISO format YYYY-MM-DD",
        ) from e

    tbl = pq.read_table(parquet_p)
    df_syms = tbl.column("symbol").to_pylist()
    df_from = tbl.column("from").to_pylist()
    df_to = tbl.column("to").to_pylist()
    df_cid = tbl.column("canonical_id").to_pylist()

    active_members: list[str] = []
    canonical_members: list[str] = []
    unmapped_symbols: list[str] = []

    for s, f_d, t_d, cid in zip(df_syms, df_from, df_to, df_cid, strict=True):
        if f_d <= as_of_str and (t_d is None or t_d == "" or as_of_str < t_d):
            active_members.append(s)
            if cid:
                canonical_members.append(cid)
            else:
                unmapped_symbols.append(s)

    sorted_active = sorted(list(set(active_members)))
    sorted_canonical = sorted(list(set(canonical_members)))
    sorted_unmapped = sorted(list(set(unmapped_symbols)))

    total = len(sorted_active)
    mapped_cnt = len(sorted_canonical)
    unmapped_cnt = len(sorted_unmapped)
    cov_pct = (mapped_cnt / total * 100.0) if total > 0 else 0.0

    return {
        "name": name,
        "as_of": as_of_str,
        "total_members": total,
        "mapped_count": mapped_cnt,
        "unmapped_count": unmapped_cnt,
        "coverage_pct": round(cov_pct, 2),
        "members": sorted_active,
        "canonical_members": sorted_canonical,
        "unmapped_symbols": sorted_unmapped,
    }


def resolve_group_universe(
    name: str,
    groups_dir: Path | str | None = None,
    manifest: Any | None = None,
    as_of: str | date | None = None,
    universe_dir: Path | str | None = None,
) -> tuple[list[str], str]:
    """Resolve group members to canonical InstrumentIds and compute deterministic content hash.

    Supports static groups and date-resolved history universes.
    """
    if as_of is not None:
        hist_res = resolve_history_group(name, as_of=as_of, universe_dir=universe_dir)
        sorted_canonical = hist_res["canonical_members"]
        payload = "\n".join(sorted_canonical).encode("utf-8")
        content_hash = hashlib.sha256(payload).hexdigest()[:16]
        return (sorted_canonical, content_hash)

    # Check if a history parquet exists with this name
    u_dir = _resolve_universe_dir(universe_dir)
    if (u_dir / f"{name}.parquet").is_file():
        # Fallback as-of today
        today_iso = date.today().isoformat()
        hist_res = resolve_history_group(name, as_of=today_iso, universe_dir=universe_dir)
        sorted_canonical = hist_res["canonical_members"]
        payload = "\n".join(sorted_canonical).encode("utf-8")
        content_hash = hashlib.sha256(payload).hexdigest()[:16]
        return (sorted_canonical, content_hash)

    from algodesk.data.aliases import resolve_alias

    raw_members = get_group(name, groups_dir)
    if raw_members is None:
        known = sorted(load_groups(groups_dir).keys())
        raise ConfigError(
            what=f"Group '{name}' not found",
            where=f"config/groups/{name}.toml",
            fix=f"Available groups: {', '.join(known)}",
        )

    resolved: list[str] = []
    for m in raw_members:
        canonical = str(resolve_alias(m, manifest=manifest))
        resolved.append(canonical)

    sorted_unique = sorted(list(set(resolved)))
    payload = "\n".join(sorted_unique).encode("utf-8")
    content_hash = hashlib.sha256(payload).hexdigest()[:16]
    return (sorted_unique, content_hash)
