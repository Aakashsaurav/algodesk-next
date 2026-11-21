"""BacktestRunner: orchestrates backtest execution by composing SessionRunner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from algodesk.api.kinds import Mode
from algodesk.api.protocols import Clock
from algodesk.config.models import RunConfig
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import Runtime


class BacktestRunner:
    """Runner for backtest execution composing SessionRunner.

    Does not copy or reimplement risk, OMS, or portfolio ledger logic;
    all trading state, lifecycle management, and event handling are delegated
    directly to the composed SessionRunner.
    """

    def __init__(
        self,
        runtime: Runtime,
        run_config: RunConfig | None = None,
        run_name: str | None = None,
        seed: int | None = None,
        dry_run: bool = False,
        clock: Clock | None = None,
        journal_db: Path | None = None,
        run_id: str | None = None,
        quiet: bool = False,
        feed: Any | None = None,
        fidelity: str | None = None,
        replay_session: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.run_name = run_name
        self.session_runner = SessionRunner(
            runtime=runtime,
            run_config=run_config,
            run_name=run_name,
            mode=Mode.BACKTEST,
            seed=seed,
            dry_run=dry_run,
            clock=clock,
            journal_db=journal_db,
            run_id=run_id,
            quiet=quiet,
            feed=feed,
            fidelity=fidelity,
            replay_session=replay_session,
        )
        self._install_daily_mark()

    def _install_daily_mark(self) -> None:
        """Ensure backtest records one mark-to-market equity row per session close."""
        sr = self.session_runner
        orig_direct = sr._snapshot_portfolio_direct
        current_date: list[str | None] = [None]
        pending_mark: list[tuple[dict[str, Any], Clock, Any] | None] = [None]

        def _daily_mark(c: dict[str, Any], clock: Clock, journal: Any) -> None:
            from algodesk.logs.formatting import format_ist

            d_str = format_ist(clock.now())[:10]
            if current_date[0] is not None and d_str != current_date[0]:
                if pending_mark[0] is not None:
                    p_c, p_clk, p_j = pending_mark[0]
                    orig_direct(p_c, p_clk, p_j)
                    pending_mark[0] = None
            current_date[0] = d_str
            pending_mark[0] = (c, clock, journal)

        sr._snapshot_portfolio_direct = _daily_mark  # type: ignore[method-assign]

    @property
    def mode(self) -> Mode:
        return self.session_runner.mode

    @property
    def run_id(self) -> str:
        return self.session_runner.run_id

    def run(self) -> Path:
        """Execute backtest session via the composed SessionRunner."""
        import asyncio
        import inspect

        res = self.session_runner.run()
        if inspect.iscoroutine(res):
            ret: Path = asyncio.run(res)
        else:
            ret = res  # type: ignore[assignment]
        self._normalize_trades_session_dates(ret)
        self._post_run_index(ret)
        return ret

    def _normalize_trades_session_dates(self, res_path: Path) -> None:
        """Preserve real quote time in trades.csv ts_ist conforming to ADR-085."""
        trades_csv = res_path / "trades.csv"
        if not trades_csv.is_file():
            return
        import csv

        try:
            with open(trades_csv, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            if not rows:
                return
            for r in rows:
                _ = r.get("ts_ist", "")
        except Exception:
            pass

    def _post_run_index(self, res_path: Path) -> None:
        """Upsert backtest run record into SQLite results index."""
        try:
            if not isinstance(self.runtime.root, Path):
                return
            from algodesk.results.index import ResultsIndex

            db_path = self.runtime.root / "data" / "state" / "results_index.db"
            idx = ResultsIndex(db_path)
            idx.upsert_run(res_path)
        except Exception:
            pass
