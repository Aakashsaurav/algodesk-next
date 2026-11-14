"""Screen run results writer producing immutable results/<run_id>/ bundle."""

from __future__ import annotations

import csv
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from algodesk.api.screens import ScreenResult

logger = logging.getLogger("algodesk.results.screen_writer")


def write_screen_results(
    results_dir: Path | str,
    result: ScreenResult,
    effective_config_text: str = "",
) -> Path:
    """Write all screen execution artifacts to results/<run_id>/."""
    root_dir = Path(results_dir)
    run_dir = root_dir / result.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    _write_summary_md(run_dir / "SUMMARY.md", result)
    _write_table_csv(run_dir / "table.csv", result)
    _write_skipped_csv(run_dir / "skipped.csv", result)
    _write_manifest_json(run_dir / "manifest.json", result)
    _write_config_effective_txt(run_dir / "config.effective.txt", effective_config_text)
    _write_metrics_json(run_dir / "metrics.json", result)

    return run_dir


def _write_summary_md(path: Path, result: ScreenResult) -> None:
    passed_count = sum(1 for r in result.rows if r.passed)
    total_symbols = len(result.rows) + len(result.skipped)
    skipped_count = len(result.skipped)

    skip_summary = ""
    if result.skipped:
        reasons: dict[str, int] = {}
        for _, reason, _ in result.skipped:
            r_str = str(reason.value if hasattr(reason, "value") else reason)
            reasons[r_str] = reasons.get(r_str, 0) + 1
        skip_summary = " (" + ", ".join(f"{k}: {v}" for k, v in sorted(reasons.items())) + ")"

    adjust_mode = result.flags.get("adjust", "splits")
    accept_unverified = bool(result.flags.get("accept_unverified", False))

    verdict_line = f"Verdict: {result.verdict}."
    if result.fail_reason:
        verdict_line = f"Verdict: {result.verdict}. Reason: {result.fail_reason}."

    unverified_note = ""
    if accept_unverified:
        unverified_note = " Unverified data accepted via --accept-unverified."

    # Compute real start time from run_id
    start_ist_str = "unknown"
    try:
        import re
        from zoneinfo import ZoneInfo

        m = re.search(r"(\d{8}_\d{6})$", result.run_id)
        if m:
            ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
            ist = ts.astimezone(ZoneInfo("Asia/Kolkata"))
            start_ist_str = ist.strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception:
        pass

    content = f"""# Screen Run: {result.name}

Screen: {result.name} ({result.timeframe}, adjust: {adjust_mode}).
Date: {result.as_of}
Mode: screen
Start: {start_ist_str}
{verdict_line}

Universe: {total_symbols} symbols.
Passed: {passed_count} symbols.
Skipped: {skipped_count}{skip_summary}.
Flags: adjust={adjust_mode}, accept_unverified={accept_unverified}.{unverified_note}

Adjust basis "{adjust_mode}": splits=split-adjusted only, total=splits+dividends, none=raw prices.

Rank percentile: Percentage of evaluated non-skipped symbols with metric value
less than or equal to current symbol value.

Caveats: Survivorship bias applies (point-in-time membership pending M4);
prices use today's corporate action adjustments.
"""
    # Enforce word limit <= 150 words
    words = content.split()
    if len(words) > 150:
        logger.warning("SUMMARY.md exceeds 150 words (%d words); truncating", len(words))
        content = " ".join(words[:150])

    path.write_text(content.strip() + "\n", encoding="utf-8")


def _write_table_csv(path: Path, result: ScreenResult) -> None:
    # Filter passed rows and sort by score descending
    passed_rows = [r for r in result.rows if r.passed]
    passed_rows.sort(key=lambda r: (r.score, r.symbol), reverse=True)

    # Collect all value keys across passed rows
    all_keys: list[str] = []
    for r in passed_rows:
        for k in r.values.keys():
            if k not in all_keys:
                all_keys.append(k)

    fieldnames = ["symbol", "passed", "score", *all_keys]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in passed_rows:
            row_dict: dict[str, Any] = {
                "symbol": r.symbol,
                "passed": r.passed,
                "score": round(r.score, 4),
            }
            for k in all_keys:
                val = r.values.get(k)
                row_dict[k] = round(val, 4) if isinstance(val, (int, float)) else (val or "")
            writer.writerow(row_dict)


def _write_skipped_csv(path: Path, result: ScreenResult) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "reason", "detail"])
        for sym, reason, detail in sorted(result.skipped, key=lambda x: x[0]):
            r_str = reason.value if hasattr(reason, "value") else str(reason)
            writer.writerow([sym, r_str, detail])


def _write_manifest_json(path: Path, result: ScreenResult) -> None:
    passed_count = sum(1 for r in result.rows if r.passed)
    total_count = len(result.rows) + len(result.skipped)
    skipped_count = len(result.skipped)

    skipped_reasons: dict[str, int] = {}
    for _, reason, _ in result.skipped:
        r_str = reason.value if hasattr(reason, "value") else str(reason)
        skipped_reasons[r_str] = skipped_reasons.get(r_str, 0) + 1

    manifest_data = {
        "run_id": result.run_id,
        "run_type": "screen",
        "name": result.name,
        "as_of": str(result.as_of),
        "timeframe": result.timeframe,
        "result_hash": result.result_hash,
        "universe_hash": result.universe_hash,
        "provenance": dict(result.provenance),
        "parameters": dict(result.parameters),
        "flags": dict(result.flags),
        "stats": {
            "universe_size": total_count,
            "passed": passed_count,
            "skipped": skipped_count,
            "skipped_pct": round(
                (skipped_count / total_count * 100.0) if total_count > 0 else 0.0, 2
            ),
            "skipped_by_reason": skipped_reasons,
            "verdict": result.verdict,
            "fail_reason": result.fail_reason,
        },
    }
    path.write_text(json.dumps(manifest_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_config_effective_txt(path: Path, effective_config_text: str) -> None:
    path.write_text(effective_config_text or "# Effective configuration\n", encoding="utf-8")


def _write_metrics_json(path: Path, result: ScreenResult) -> None:
    # Compute real start time from run_id timestamp
    start_ist_str = None
    try:
        import re
        from zoneinfo import ZoneInfo

        m = re.search(r"(\d{8}_\d{6})$", result.run_id)
        if m:
            ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
            ist = ts.astimezone(ZoneInfo("Asia/Kolkata"))
            start_ist_str = ist.strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception:
        pass

    metrics_data = {
        "run_id": result.run_id,
        "run_type": "screen",
        "screen": result.name,
        "as_of": str(result.as_of),
        "date": str(result.as_of),
        "mode": "screen",
        "trades": 0,
        "net_pnl": 0.0,
        "verdict": result.verdict,
        "fail_reason": result.fail_reason,
        "start_ist": start_ist_str,
    }
    path.write_text(json.dumps(metrics_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
