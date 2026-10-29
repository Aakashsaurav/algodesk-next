"""Compare results runs and ordered sequences of journal events.

Compares summary metrics, signals, risk decisions, orders, and fills between
two execution runs to verify determinism and replay accuracy.
"""

from __future__ import annotations

import json
import math
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
                # pyrefly: ignore[implicit-any-empty-container]
                payload = {}

        if kind == "signal":
            signals.append(
                {
                    "strategy": payload.get("strategy"),
                    "instrument": str(payload.get("instrument")),
                    "side": str(payload.get("side")),
                    "qty": int(payload.get("qty", 0)),
                }
            )
        elif kind == "risk.decision":
            decisions.append(
                {
                    "allow": bool(payload.get("allow")),
                    "rule": payload.get("rule"),
                    "reason": payload.get("reason"),
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


def _compare_sequence_items(
    name: str,
    seq_a: list[dict[str, Any]],
    seq_b: list[dict[str, Any]],
) -> str | None:
    """Compare two ordered sequences of event items and return first difference."""
    if len(seq_a) != len(seq_b):
        return f"{name} count: {len(seq_a)} != {len(seq_b)}"
    for idx, (item_a, item_b) in enumerate(zip(seq_a, seq_b, strict=True)):
        if item_a != item_b:
            return f"{name}[{idx}]: {item_a} != {item_b}"
    return None


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
    if diff_metric is not None:
        return False, diff_metric

    # 2. Extract and compare journal event sequences
    events_a = _load_events_for_run(r_path, run_a, dir_a)
    events_b = _load_events_for_run(r_path, run_b, dir_b)
    seqs_a = _extract_sequences(events_a)
    seqs_b = _extract_sequences(events_b)

    for seq_name in ("signals", "risk_decisions", "orders", "fills"):
        diff_seq = _compare_sequence_items(seq_name, seqs_a[seq_name], seqs_b[seq_name])
        if diff_seq is not None:
            return False, diff_seq

    return True, None
