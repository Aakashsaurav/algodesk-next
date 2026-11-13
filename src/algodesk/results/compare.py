"""Compare results runs and ordered sequences of journal events.

Compares summary metrics, signals, risk decisions, orders, and fills between
two execution runs to verify determinism and replay accuracy.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from algodesk.api.errors import ConfigError
from algodesk.journal.store import JournalReader


def _load_events_for_run(root: Path, run_id: str, run_dir: Path) -> list[dict[str, Any]]:
    """Load journal events for a given run from events.jsonl or SQLite databases."""
    events_jsonl = run_dir / "events.jsonl"
    if events_jsonl.is_file():
        events: list[dict[str, Any]] = []
        for line in events_jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    # Check state journal databases
    state_dir = root / ".algodesk" / "state"
    candidate_dbs = [
        state_dir / "journal_replay.db",
        state_dir / "journal.db",
    ]
    if state_dir.is_dir():
        for p in state_dir.glob("journal*.db"):
            if p not in candidate_dbs:
                candidate_dbs.append(p)

    for db_path in candidate_dbs:
        if db_path.is_file():
            try:
                reader = JournalReader(db_path)
                found = reader.events(run_id=run_id)
                if found:
                    return found
            except Exception:
                continue

    return []


def _extract_sequences(events: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Extract ordered sequences of signals, risk decisions, orders, and fills."""
    signals: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []

    for ev in events:
        kind = ev.get("kind")
        payload = ev.get("payload_json", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        ev_ts = payload.get("ts") or payload.get("created_ts") or ev.get("ts_utc")

        if kind == "signal":
            signals.append(
                {
                    "strategy": payload.get("strategy"),
                    "instrument": str(payload.get("instrument")),
                    "side": str(payload.get("side")),
                    "qty": int(payload.get("qty", 0)),
                    "intent_id": payload.get("intent_id"),
                    "_ts": ev_ts,
                    "_price": payload.get("price", "N/A"),
                }
            )
        elif kind == "risk.decision":
            decisions.append(
                {
                    "allow": bool(payload.get("allow")),
                    "rule": payload.get("rule"),
                    "reason": payload.get("reason"),
                    "_ts": ev_ts,
                    "_price": "N/A",
                }
            )
        elif kind == "order.created":
            spec = payload.get("spec", {})
            orders.append(
                {
                    "instrument": str(spec.get("instrument")),
                    "side": str(spec.get("side")),
                    "qty": int(spec.get("qty", 0)),
                    "type": str(spec.get("type")),
                    "price": str(spec.get("price")),
                    "product": str(spec.get("product")),
                    "_ts": ev_ts,
                    "_price": spec.get("price"),
                }
            )
        elif kind == "fill":
            fills.append(
                {
                    "instrument": str(payload.get("instrument")),
                    "side": str(payload.get("side")),
                    "qty": int(payload.get("qty", 0)),
                    "price": str(payload.get("price")),
                    "charges_total": str(payload.get("charges_total")),
                    "_ts": ev_ts,
                    "_price": payload.get("price"),
                }
            )

    return {
        "signals": signals,
        "risk_decisions": decisions,
        "orders": orders,
        "fills": fills,
    }


def _compare_metrics(
    m_a: dict[str, Any],
    m_b: dict[str, Any],
) -> str | None:
    """Compare key trading metrics between run A and run B."""
    metric_keys = ["trades", "gross_pnl", "net_pnl", "charges", "win_rate", "max_drawdown"]
    for key in metric_keys:
        if key not in m_a or key not in m_b:
            continue
        val_a = m_a[key]
        val_b = m_b[key]
        if isinstance(val_a, (int, float)) and isinstance(val_b, (int, float)):
            if not math.isclose(float(val_a), float(val_b), abs_tol=1e-5):
                return f"metrics.{key}: {val_a} != {val_b}"
        elif val_a != val_b:
            return f"metrics.{key}: {val_a} != {val_b}"
    return None


def _item_semantic_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Compare items ignoring internal tracking metadata (_ts, _price)."""
    keys = (set(a.keys()) | set(b.keys())) - {"_ts", "_price"}
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if k == "reason" and isinstance(va, str) and isinstance(vb, str):
            norm_a = re.sub(r"Quote age \d+\.\d+s", "Quote age <age>s", va)
            norm_b = re.sub(r"Quote age \d+\.\d+s", "Quote age <age>s", vb)
            if norm_a != norm_b:
                return False
            continue
        if k == "intent_id" and isinstance(va, str) and isinstance(vb, str):
            norm_a = re.sub(r"^(timer_exit_.*_\d+)\.\d+$", r"\1", va)
            norm_b = re.sub(r"^(timer_exit_.*_\d+)\.\d+$", r"\1", vb)
            if norm_a != norm_b:
                return False
            continue
        if va != vb:
            return False
    return True


def _format_item_diff(
    name: str,
    idx: int,
    item_a: dict[str, Any] | None,
    item_b: dict[str, Any] | None,
) -> str:
    if item_a is None and item_b is not None:
        p_b = item_b.get("_price")
        ts_b = item_b.get("_ts")
        return f"{name}[{idx}]: A [missing] != B [ts={ts_b}, price={p_b}]"
    if item_a is not None and item_b is None:
        p_a = item_a.get("_price")
        ts_a = item_a.get("_ts")
        return f"{name}[{idx}]: A [ts={ts_a}, price={p_a}] != B [missing]"
    assert item_a is not None and item_b is not None
    ts_a = item_a.get("_ts")
    ts_b = item_b.get("_ts")
    price_a = item_a.get("_price")
    price_b = item_b.get("_price")
    inst_a = item_a.get("instrument", "")
    inst_b = item_b.get("instrument", "")
    side_a = item_a.get("side", "")
    side_b = item_b.get("side", "")
    return (
        f"{name}[{idx}]: A [ts={ts_a}, price={price_a}, inst={inst_a}, side={side_a}] "
        f"!= B [ts={ts_b}, price={price_b}, inst={inst_b}, side={side_b}]"
    )


def _collect_sequence_diffs(
    name: str,
    seq_a: list[dict[str, Any]],
    seq_b: list[dict[str, Any]],
    diff_count: int = 1,
) -> list[str]:
    """Collect up to diff_count differences for a sequence."""
    diffs: list[str] = []
    common_len = min(len(seq_a), len(seq_b))
    for idx in range(common_len):
        if not _item_semantic_equal(seq_a[idx], seq_b[idx]):
            diffs.append(_format_item_diff(name, idx, seq_a[idx], seq_b[idx]))
            if len(diffs) >= diff_count:
                return diffs
    if len(seq_a) != len(seq_b):
        for idx in range(common_len, max(len(seq_a), len(seq_b))):
            item_a = seq_a[idx] if idx < len(seq_a) else None
            item_b = seq_b[idx] if idx < len(seq_b) else None
            diff_str = _format_item_diff(name, idx, item_a, item_b)
            if not diffs and idx == common_len:
                diff_str = f"{name} count ({len(seq_a)} != {len(seq_b)}): {diff_str}"
            diffs.append(diff_str)
            if len(diffs) >= diff_count:
                break
    return diffs


def _compare_sequence_items(
    name: str,
    seq_a: list[dict[str, Any]],
    seq_b: list[dict[str, Any]],
) -> str | None:
    """Compare two ordered event lists and return first difference with timestamps/prices."""
    diffs = _collect_sequence_diffs(name, seq_a, seq_b, diff_count=1)
    if diffs:
        if len(seq_a) != len(seq_b) and min(len(seq_a), len(seq_b)) == 0:
            return f"{name} count ({len(seq_a)} != {len(seq_b)}): {diffs[0]}"
        return diffs[0]
    return None


def _get_configured_plugins(run_dir: Path) -> set[str] | None:
    """Extract plugins configured for a run from manifest.json or config.effective.txt."""
    man_path = run_dir / "manifest.json"
    if man_path.is_file():
        try:
            man = json.loads(man_path.read_text(encoding="utf-8"))
            if "configured_plugins" in man and isinstance(man["configured_plugins"], list):
                return set(man["configured_plugins"])
        except Exception:
            pass

    eff_path = run_dir / "config.effective.txt"
    if eff_path.is_file():
        try:
            content = eff_path.read_text(encoding="utf-8")
            matches = re.findall(r"(?:plugin|schedule|screen)\s*=\s*['\"]([^'\"]+)['\"]", content)
            if matches:
                return set(matches)
        except Exception:
            pass
    return None


def _format_version_differences(dir_a: Path, dir_b: Path) -> list[str]:
    """Compare code and plugin versions between two manifests for configured plugins."""
    diffs: list[str] = []
    man_a_path = dir_a / "manifest.json"
    man_b_path = dir_b / "manifest.json"
    if not man_a_path.is_file() or not man_b_path.is_file():
        return diffs
    try:
        man_a = json.loads(man_a_path.read_text(encoding="utf-8"))
        man_b = json.loads(man_b_path.read_text(encoding="utf-8"))
    except Exception:
        return diffs

    cv_a = man_a.get("code_version")
    cv_b = man_b.get("code_version")
    if cv_a and cv_b and cv_a != cv_b:
        diffs.append(f"code_version: {cv_a} != {cv_b}")

    pv_a = man_a.get("plugin_versions") or {}
    pv_b = man_b.get("plugin_versions") or {}

    active_a = _get_configured_plugins(dir_a)
    active_b = _get_configured_plugins(dir_b)
    configured_filter: set[str] | None = None
    if active_a is not None or active_b is not None:
        configured_filter = (active_a or set()) | (active_b or set())

    all_plugins = sorted(set(pv_a.keys()) | set(pv_b.keys()))
    for p_name in all_plugins:
        if configured_filter is not None and p_name not in configured_filter:
            continue
        info_a = pv_a.get(p_name, {})
        info_b = pv_b.get(p_name, {})
        va, vb = info_a.get("version"), info_b.get("version")
        ha, hb = info_a.get("hash"), info_b.get("hash")
        if va != vb:
            diffs.append(f"plugin '{p_name}' version: {va} != {vb}")
        elif ha != hb:
            diffs.append(f"plugin '{p_name}' code hash: {ha} != {hb}")

    return diffs


def _attach_version_diffs(dir_a: Path, dir_b: Path, base_msg: str) -> str:
    v_diffs = _format_version_differences(dir_a, dir_b)
    if not v_diffs:
        return base_msg
    return f"{base_msg} | versions: {', '.join(v_diffs)}"


def compare_runs(
    root: Path | str,
    run_a: str,
    run_b: str,
) -> tuple[bool, str | None]:
    """Compare two execution runs by metrics and ordered journal sequences."""
    r_path = Path(root).resolve()
    dir_a = r_path / "results" / run_a if not Path(run_a).is_dir() else Path(run_a)
    dir_b = r_path / "results" / run_b if not Path(run_b).is_dir() else Path(run_b)

    if not dir_a.is_dir():
        raise ConfigError(
            what=f"Results bundle '{run_a}' not found",
            where=str(dir_a),
            fix="Use 'algodesk results list' to inspect available runs",
        )
    if not dir_b.is_dir():
        raise ConfigError(
            what=f"Results bundle '{run_b}' not found",
            where=str(dir_b),
            fix="Use 'algodesk results list' to inspect available runs",
        )

    # 1. Compare metrics
    m_path_a = dir_a / "metrics.json"
    m_path_b = dir_b / "metrics.json"
    m_a = json.loads(m_path_a.read_text(encoding="utf-8")) if m_path_a.is_file() else {}
    m_b = json.loads(m_path_b.read_text(encoding="utf-8")) if m_path_b.is_file() else {}
    diff_metric = _compare_metrics(m_a, m_b)

    # 2. Extract and compare journal event sequences
    events_a = _load_events_for_run(r_path, run_a, dir_a)
    events_b = _load_events_for_run(r_path, run_b, dir_b)
    seqs_a = _extract_sequences(events_a)
    seqs_b = _extract_sequences(events_b)

    seq_names = ("signals", "risk_decisions", "orders", "fills")
    counts = {s_name: (len(seqs_a[s_name]), len(seqs_b[s_name])) for s_name in seq_names}
    seq_diffs = {
        s_name: _collect_sequence_diffs(s_name, seqs_a[s_name], seqs_b[s_name], diff_count=3)
        for s_name in seq_names
    }

    has_diffs = any(bool(d) for d in seq_diffs.values())
    if has_diffs or diff_metric is not None:
        first_diff: str | None = None
        for s_name in ("signals", "orders", "fills", "risk_decisions"):
            if seq_diffs.get(s_name):
                first_diff = seq_diffs[s_name][0]
                break
        if first_diff is None:
            first_diff = diff_metric or "metrics difference"
        elif diff_metric:
            first_diff = f"{first_diff} ({diff_metric})"

        counts_str = ", ".join(f"{k} (A={v[0]}, B={v[1]})" for k, v in counts.items())
        diff_lines = [first_diff, f"counts: {counts_str}"]
        for s_name in seq_names:
            diff_list = seq_diffs.get(s_name, [])
            if diff_list:
                diff_lines.append(f"{s_name} differences (showing first {len(diff_list)}):")
                for d in diff_list:
                    diff_lines.append(f"  {d}")
        detail = "\n".join(diff_lines)
        return False, _attach_version_diffs(dir_a, dir_b, detail)

    v_diffs = _format_version_differences(dir_a, dir_b)
    if v_diffs:
        return False, f"Code or plugin versions differ: {', '.join(v_diffs)}"

    return True, None
