"""SQLite results index storing run metadata, hashes, and headline metrics."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger("algodesk.results.index")


class ResultsIndex:
    """Manages the persistent SQLite results index at data/state/results_index.db."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create the results_index schema if not present."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS results_index (
                    run_id TEXT PRIMARY KEY,
                    run_type TEXT NOT NULL,
                    strategy TEXT,
                    study TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    result_hash TEXT,
                    trades INTEGER,
                    net_pnl REAL,
                    win_rate REAL,
                    max_drawdown REAL,
                    cagr REAL,
                    sharpe REAL,
                    verdict TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_results_type ON results_index(run_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_results_study ON results_index(study)")

    def upsert_run(self, run_dir: Path) -> bool:
        """Read a run directory and insert or update its record in the index."""
        if not run_dir.is_dir():
            return False

        m_file = run_dir / "metrics.json"
        s_file = run_dir / "SUMMARY.md"
        man_file = run_dir / "manifest.json"

        if not m_file.is_file() and not s_file.is_file():
            return False

        m_data: dict[str, Any] = {}
        if m_file.is_file():
            try:
                m_data = json.loads(m_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        man_data: dict[str, Any] = {}
        if man_file.is_file():
            try:
                man_data = json.loads(man_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        r_id = run_dir.name
        run_type = str(
            m_data.get("mode")
            or man_data.get("mode")
            or ("backtest" if "backtest" in r_id else "paper")
        ).lower()
        if m_data.get("run_type") == "screen":
            run_type = "screen"

        strategy = self._extract_strategy(run_dir, m_data, man_data)
        study = m_data.get("study") or man_data.get("study")

        start_date = str(
            m_data.get("first_tick_ist") or m_data.get("first_tick_utc") or m_data.get("date") or ""
        )[:10]
        end_date = str(
            m_data.get("last_tick_ist") or m_data.get("last_tick_utc") or m_data.get("date") or ""
        )[:10]

        res_hash = man_data.get("result_hash") or ""
        trades = int(m_data.get("trades", 0)) if run_type != "screen" else 0
        net_pnl = float(m_data.get("net_pnl", 0.0)) if run_type != "screen" else 0.0
        win_rate = float(m_data.get("win_rate", 0.0)) if run_type != "screen" else 0.0
        max_dd = float(m_data.get("max_drawdown", 0.0)) if run_type != "screen" else 0.0
        cagr = float(m_data.get("cagr", 0.0))
        sharpe = float(m_data.get("sharpe", 0.0))
        verdict = self._extract_verdict(s_file, man_data, m_data)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO results_index (
                    run_id, run_type, strategy, study, start_date, end_date,
                    result_hash, trades, net_pnl, win_rate, max_drawdown,
                    cagr, sharpe, verdict, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    r_id,
                    run_type,
                    strategy,
                    study,
                    start_date,
                    end_date,
                    res_hash,
                    trades,
                    net_pnl,
                    win_rate,
                    max_dd,
                    cagr,
                    sharpe,
                    verdict,
                ),
            )
        return True

    def _extract_strategy(
        self,
        run_dir: Path,
        m_data: Mapping[str, Any],
        man_data: Mapping[str, Any],
    ) -> str:
        """Extract strategy name from manifest, config, or directory name."""
        strat = m_data.get("strategy") or man_data.get("strategy")
        if strat:
            return str(strat)
        cfg_file = run_dir / "config.effective.txt"
        if cfg_file.is_file():
            try:
                for line in cfg_file.read_text(encoding="utf-8").splitlines():
                    if line.strip().startswith("strategy ="):
                        return line.split("=")[-1].strip().strip('"')
            except Exception:
                pass
        parts = run_dir.name.split("_")
        return parts[0] if parts else "unknown"

    def _extract_verdict(
        self,
        s_file: Path,
        man_data: Mapping[str, Any],
        m_data: Mapping[str, Any],
    ) -> str:
        """Extract verdict from manifest, SUMMARY.md, or metrics."""
        if man_data.get("verdict"):
            return str(man_data["verdict"])
        if m_data.get("verdict"):
            return str(m_data["verdict"])
        if s_file.is_file():
            try:
                for line in s_file.read_text(encoding="utf-8").splitlines():
                    if "Verdict:" in line:
                        return line.split("Verdict:")[-1].strip().split(".")[0]
            except Exception:
                pass
        return "PASS"

    def rebuild(self, results_dir: Path) -> int:
        """Scan results directory and rebuild the entire SQLite index."""
        if not results_dir.is_dir():
            return 0
        count = 0
        for p in results_dir.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                if self.upsert_run(p):
                    count += 1
        return count

    def list_runs(
        self,
        run_type: str | None = None,
        study: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query indexed runs with optional type and study filtering."""
        query = "SELECT * FROM results_index WHERE 1=1"
        params: list[Any] = []
        if run_type:
            query += " AND run_type = ?"
            params.append(run_type.lower())
        if study:
            query += " AND study = ?"
            params.append(study)
        query += " ORDER BY run_id DESC"

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Fetch indexed record for a specific run ID."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM results_index WHERE run_id = ?", (run_id,)).fetchone()
            return dict(row) if row else None

    def delete_run(self, run_id: str) -> bool:
        """Remove a run entry from the SQLite index."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM results_index WHERE run_id = ?", (run_id,))
            return cur.rowcount > 0

    def count_study_trials(self, study: str) -> int:
        """Count existing backtest trial runs for a study."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM results_index WHERE study = ? AND run_type = 'backtest'",
                (study,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
