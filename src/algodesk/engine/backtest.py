"""BacktestRunner: orchestrates backtest execution by composing SessionRunner."""

from __future__ import annotations

from pathlib import Path

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
    ) -> None:
        self.runtime = runtime
        self.run_name = run_name
        self.session_runner = SessionRunner(
            runtime=runtime,
            run_config=run_config,
            mode=Mode.BACKTEST,
            seed=seed,
            dry_run=dry_run,
            clock=clock,
            journal_db=journal_db,
            run_id=run_id,
            quiet=quiet,
        )

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
            return ret
        return res  # type: ignore[return-value]
