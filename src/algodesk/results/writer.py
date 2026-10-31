"""Results writer generating session artifacts, metrics, and summary documents."""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger("algodesk.results.writer")


def compute_max_drawdown(equity_curve: Sequence[float]) -> float:
    """Calculate maximum peak-to-trough percentage drawdown from equity series."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
    return round(max_dd * 100.0, 4)


class ResultsWriter:
    """Writes run artifacts to results/<run_id>/ directory."""

    def __init__(self, run_id: str, results_dir: Path | str = "results") -> None:
        self.run_id = run_id
        self.results_dir = Path(results_dir)
        self.run_dir = self.results_dir / run_id

    def write(
        self,
        summary_info: Mapping[str, Any],
        metrics: Mapping[str, Any],
        trades: Sequence[Mapping[str, Any]],
        equity_points: Sequence[Mapping[str, Any]],
        effective_config_text: str,
        registry_snapshot: Mapping[str, Any],
        manifest_info: Mapping[str, Any],
        session_text: str | None = None,
    ) -> Path:
        """Write all session artifacts. Writing succeeds even if partial failures occur."""
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error("Failed to create results directory %s: %s", self.run_dir, exc)
            return self.run_dir

        self._write_summary(summary_info, metrics)
        self._write_metrics(metrics)
        self._write_trades_csv(trades)
        self._write_equity_csv(equity_points)
        self._write_text_file("config.effective.txt", effective_config_text)
        self._write_json_file("registry.snapshot.json", registry_snapshot)
        self._write_json_file("manifest.json", manifest_info)
        self._write_session_md(session_text, summary_info, metrics)

        return self.run_dir

    def _write_summary(
        self,
        summary_info: Mapping[str, Any],
        metrics: Mapping[str, Any],
    ) -> None:
        """Generate SUMMARY.md with concise execution metrics and paper disclaimer."""
        run_name = summary_info.get("run_name", self.run_id)
        mode = summary_info.get("mode", "paper")
        date_str = summary_info.get("date", "2026-10-26")
        net_pnl = float(metrics.get("net_pnl", 0.0))
        gross_pnl = float(metrics.get("gross_pnl", 0.0))
        charges = float(metrics.get("charges", 0.0))
        trade_count = int(metrics.get("trades", 0))
        win_rate = float(metrics.get("win_rate", 0.0))
        max_dd = float(metrics.get("max_drawdown", 0.0))
        verdict = summary_info.get("verdict", "PASS")
        fail_reason = summary_info.get("fail_reason")
        fail_clause = f"Reason: {fail_reason}. " if fail_reason else ""

        content = (
            f"# Run Summary: {self.run_id}\n\n"
            "## Summary\n"
            f"AlgoDesk Next {mode} session '{run_name}' executed on {date_str}. "
            f"Net P&L: ₹{net_pnl:.2f} (Gross: ₹{gross_pnl:.2f}, Charges: ₹{charges:.2f}) "
            f"across {trade_count} trades with {win_rate:.1f}% win rate and max DD of "
            f"{max_dd:.2f}%. Verdict: {verdict}. {fail_clause}"
            "Note: Simulated paper fills assume zero impact.\n"
        )
        self._write_text_file("SUMMARY.md", content)

    def _write_metrics(self, metrics: Mapping[str, Any]) -> None:
        """Serialize session metrics to metrics.json."""
        default_metrics = {
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "charges": 0.0,
            "trades": 0,
            "win_rate": 0.0,
            "max_drawdown": 0.0,
            "exposure_time": 0.0,
            # pyrefly: ignore[implicit-any-empty-container]
            "rejects_by_rule": {},
            # pyrefly: ignore[implicit-any-empty-container]
            "ticks_per_instrument": {},
            "feed_gaps": 0,
            "killswitch_events": 0,
        }
        merged = {**default_metrics, **metrics}
        self._write_json_file("metrics.json", merged)

    def _write_trades_csv(self, trades: Sequence[Mapping[str, Any]]) -> None:
        """Serialize trade fills to trades.csv."""
        fieldnames = [
            "trade_id",
            "fill_id",
            "client_ref",
            "instrument",
            "side",
            "qty",
            "price",
            "charges",
            "ts",
        ]
        file_path = self.run_dir / "trades.csv"
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for tr in trades:
                    row = {k: tr.get(k, "") for k in fieldnames}
                    writer.writerow(row)
        except Exception as exc:
            logger.warning("Failed writing trades.csv: %s", exc)

    def _write_equity_csv(self, equity_points: Sequence[Mapping[str, Any]]) -> None:
        """Serialize equity curve snapshots to equity.csv."""
        fieldnames = ["ts", "equity", "cash", "unrealized", "realized", "drawdown"]
        file_path = self.run_dir / "equity.csv"
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for pt in equity_points:
                    row = {k: pt.get(k, "") for k in fieldnames}
                    writer.writerow(row)
        except Exception as exc:
            logger.warning("Failed writing equity.csv: %s", exc)

    def _write_session_md(
        self,
        session_text: str | None,
        summary_info: Mapping[str, Any],
        metrics: Mapping[str, Any],
    ) -> None:
        """Write session summary text for notifications and auditing."""
        if session_text:
            content = session_text
        else:
            run_name = summary_info.get("run_name", self.run_id)
            net_pnl = float(metrics.get("net_pnl", 0.0))
            trades = int(metrics.get("trades", 0))
            max_dd = metrics.get("max_drawdown", 0.0)
            content = (
                f"Session {run_name} completed.\n"
                f"Net P&L: ₹{net_pnl:.2f} | Trades: {trades} | Max DD: {max_dd:.2f}%\n"
                f"Status: {summary_info.get('verdict', 'COMPLETED')}"
            )
        self._write_text_file("session.md", content)

    def _write_text_file(self, filename: str, content: str) -> None:
        try:
            (self.run_dir / filename).write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed writing %s: %s", filename, exc)

    def _write_json_file(self, filename: str, data: Any) -> None:
        try:
            (self.run_dir / filename).write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Failed writing %s: %s", filename, exc)
