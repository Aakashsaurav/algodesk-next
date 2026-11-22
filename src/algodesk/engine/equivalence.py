"""Backtest equivalence execution: runs bar-fidelity backtest with TickBarFeed."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode
from algodesk.data.tick_bar_feed import TickBarFeed
from algodesk.engine.replay_config import (
    apply_recorded_registry_snapshot,
    load_recorded_session_config,
)
from algodesk.engine.runner import SessionRunner
from algodesk.results.equivalence import (
    EquivalenceResult,
    compare_equivalence_events,
    load_journal_events,
)

logger = logging.getLogger("algodesk.engine.equivalence")


def run_backtest_equivalence(rt: Any, session_id: str) -> EquivalenceResult:
    """Execute bar-fidelity backtest with TickBarFeed and verify signal equivalence."""
    sess_dir = rt.paths.results / session_id
    if not sess_dir.is_dir() and Path(session_id).is_dir():
        sess_dir = Path(session_id)
    if not sess_dir.is_dir():
        raise ConfigError(
            what=f"Recorded session '{session_id}' not found in results directory",
            where=f"results/{session_id}",
            fix="Provide a valid paper session ID",
        )

    # 1. Load paper journal events
    paper_events = load_journal_events(sess_dir)

    # 2. Reconstruct run config and registry snapshot
    run_config, _ = load_recorded_session_config(sess_dir, rt)
    snap_file = sess_dir / "registry.snapshot.json"
    if snap_file.is_file() and rt.params is not None:
        apply_recorded_registry_snapshot(snap_file, rt.params)

    # 3. Build TickBarFeed with session calendars
    from algodesk.engine.sessions import SessionManager

    cals = getattr(rt, "calendars", None)
    sm = SessionManager(calendars=cals) if cals else None
    feed = TickBarFeed(
        session_dir=sess_dir,
        data_dir=rt.paths.data,
        timeframe="1m",
        session_manager=sm,
    )

    # 4. Run bar-fidelity backtest
    bt_run_id = f"{session_id}_bt_eq"
    runner = SessionRunner(
        rt,
        run_config=run_config,
        mode=Mode.BACKTEST,
        feed=feed,
        run_id=bt_run_id,
        quiet=True,
    )

    bt_res_path = asyncio.run(runner.run())

    # 5. Load backtest journal events
    bt_events = load_journal_events(bt_res_path)

    # 6. Compare equivalence and return result
    return compare_equivalence_events(paper_events, bt_events, bt_res_path, bars=feed.bars)
