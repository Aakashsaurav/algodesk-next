"""BacktestRunner: orchestrates backtest execution by composing SessionRunner."""

from __future__ import annotations

from datetime import datetime
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
        from datetime import timedelta

        from algodesk.logs.formatting import format_ist
        from algodesk.results.writer import compute_max_drawdown

        sr = self.session_runner
        ist_offset = timedelta(hours=5, minutes=30)
        current_date: list[Any] = [None]
        pending_mark: list[tuple[dict[str, Any], Any, Any] | None] = [None]

        def _record_snapshot(
            c: dict[str, Any], ts: Any, journal: Any, quotes: dict[Any, Any]
        ) -> None:
            ledger = c.get("ledger")
            if ledger is None:
                return
            for q in quotes.values():
                ledger.mark(q)
            snap = ledger.snapshot()
            eq_val = float(snap.equity)
            tracker = getattr(sr, "_drawdown_tracker", None)
            if tracker is not None:
                max_dd, _, _ = tracker.update(eq_val, ts if isinstance(ts, datetime) else None)
            else:
                max_dd = compute_max_drawdown([p["equity"] for p in sr._equity_curve] + [eq_val])
            pt_dict = {
                "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "ts_ist": format_ist(ts),
                "equity": eq_val,
                "cash": float(snap.cash),
                "unrealized": float(ledger.unrealized),
                "realized": float(ledger.realized),
                "drawdown": max_dd,
            }
            sr._equity_curve.append(pt_dict)
            if hasattr(ledger, "set_start_of_day_baseline"):
                ledger.set_start_of_day_baseline(snap.equity)
            if journal is not None and hasattr(journal, "append"):
                journal.append(
                    "portfolio.snapshot",
                    {
                        "equity": pt_dict["equity"],
                        "cash": pt_dict["cash"],
                        "unrealized": pt_dict["unrealized"],
                        "realized": pt_dict["realized"],
                        "seq": sr._tick_seq,
                        "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    },
                )

        def _handle_event(
            c: dict[str, Any], clock: Clock, journal: Any, is_bar: bool = False
        ) -> None:
            now_ts = clock.now()
            d_val = (now_ts + ist_offset).date()
            if current_date[0] is not None and d_val != current_date[0]:
                if pending_mark[0] is not None:
                    p_c, p_ts, p_j = pending_mark[0]
                    _record_snapshot(p_c, p_ts, p_j, sr._latest_quotes)
                    pending_mark[0] = None
            current_date[0] = d_val
            if is_bar or pending_mark[0] is None:
                pending_mark[0] = (c, now_ts, journal)

        orig_stop = sr.stop

        def _stop_and_flush(*args: Any, **kwargs: Any) -> None:
            if pending_mark[0] is not None:
                p_c, p_ts, p_j = pending_mark[0]
                _record_snapshot(p_c, p_ts, p_j, sr._latest_quotes)
                pending_mark[0] = None
            orig_stop(*args, **kwargs)

        def _direct_snap(c: dict[str, Any], clock: Clock, journal: Any) -> None:
            _handle_event(c, clock, journal, is_bar=True)

        sr._on_backtest_market_event = _handle_event  # type: ignore[method-assign]
        sr._snapshot_portfolio_direct = _direct_snap  # type: ignore[method-assign]
        sr.stop = _stop_and_flush  # type: ignore[method-assign]

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
