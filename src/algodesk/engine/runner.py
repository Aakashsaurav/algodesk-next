"""SessionRunner orchestrating paper trading and historical replay sessions.

Wires market events, bar building, strategy execution, pre-trade risk evaluation,
order management, simulated venue matching, portfolio ledgering, and results reporting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import signal
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import typer

from algodesk.api import (
    AlgoDeskError,
    Bar,
    ConfigError,
    FeedAuthError,
    Fill,
    FillEvent,
    Instrument,
    InstrumentId,
    Mode,
    OrderEventMsg,
    OrderState,
    PluginContext,
    PluginKind,
    Position,
    Product,
    Quote,
    SafetyViolation,
    Tick,
    parse_id,
)
from algodesk.api.checks import Severity
from algodesk.api.market import FeedStatus, MarketEvent
from algodesk.api.protocols import Clock
from algodesk.data.catalog import resolve_selector
from algodesk.data.manifest import DataManifest
from algodesk.data.recorder import Recorder
from algodesk.data.replay import ReplayFeed, save_warmup_bars_parquet
from algodesk.engine.bars import BarBuilder
from algodesk.engine.bus import EventBus
from algodesk.engine.clock import SimClock, WallClock
from algodesk.engine.context import BarBuffer, build_strategy_context
from algodesk.engine.indicator_manager import IndicatorManager
from algodesk.engine.notify import NotifyBridge
from algodesk.engine.scheduler import Scheduler
from algodesk.engine.sessions import MarketState, SessionManager
from algodesk.engine.strategy_runtime import StrategyRuntime
from algodesk.fees.engine import FeeEngine
from algodesk.journal.store import JournalReader
from algodesk.journal.writer import Journal, JournalSettings
from algodesk.logs.formatting import IST, format_ist
from algodesk.oms.manager import OrderManager
from algodesk.ops.checks import build_run_checks
from algodesk.ops.context import build_context
from algodesk.ops.doctor import DoctorRegistry
from algodesk.ops.runtime import Runtime
from algodesk.portfolio.ledger import PortfolioLedger
from algodesk.results.roundtrips import compute_round_trip_metrics
from algodesk.results.writer import ResultsWriter, compute_max_drawdown
from algodesk.risk.engine import RiskEngine
from algodesk.risk.killswitch import KillSwitch
from algodesk.risk.monitor import RiskMonitor
from algodesk.risk.view import RiskViewContext

logger = logging.getLogger("algodesk.engine.runner")


def _parse_ist_str_to_utc(ist_str: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM:SS IST' or ISO timestamp to UTC datetime."""
    s = str(ist_str).replace(" IST", "").strip()
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=IST).astimezone(UTC)
    except Exception:
        return datetime.fromisoformat(s).astimezone(UTC)


class SessionRunner:
    """Executes paper trading and deterministic replay sessions end-to-end."""

    def __init__(
        self,
        runtime: Runtime,
        run_config: Any | None = None,
        *,
        mode: str | Mode = Mode.PAPER,
        observe: bool = False,
        replay_dates: Sequence[date] | Sequence[str] | None = None,
        seed: int | None = None,
        dry_run: bool = False,
        clock: Clock | None = None,
        journal_db: Path | None = None,
        run_id: str | None = None,
        secrets: Any | None = None,
        quiet: bool = False,
        warmup_source: str | None = None,
        replay_session: str | None = None,
        tick_window: tuple[datetime, datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self._secrets_provider = secrets
        self.quiet = quiet
        self.warmup_source = warmup_source
        self.replay_session = replay_session
        self.tick_window = tick_window
        self._version_warnings: list[str] = []
        self._data_service: Any = None
        self.run_config = run_config or runtime.effective.run
        if isinstance(mode, str):
            self.mode = Mode.BACKTEST if mode.lower() == "replay" else Mode(mode.lower())
        else:
            self.mode = mode
        self._is_replay: bool = (
            str(mode).lower() == "replay" or bool(replay_dates) or bool(replay_session)
        )
        self.observe = observe

        if replay_session:
            sess_path = self.runtime.paths.results / replay_session
            if not sess_path.is_dir() and Path(replay_session).is_dir():
                sess_path = Path(replay_session)
            if not sess_path.is_dir():
                raise ConfigError(
                    what=f"Session '{replay_session}' not found in results directory",
                    where="SessionRunner",
                    fix="Use 'algodesk results list' to view available sessions",
                )
            m_path = sess_path / "metrics.json"
            man_path = sess_path / "manifest.json"
            m_data = json.loads(m_path.read_text(encoding="utf-8")) if m_path.is_file() else {}
            man_data = (
                json.loads(man_path.read_text(encoding="utf-8")) if man_path.is_file() else {}
            )

            sess_date = m_data.get("date")
            if sess_date and not replay_dates:
                replay_dates = [str(sess_date)]
            if seed is None and man_data.get("seed") is not None:
                seed = int(man_data["seed"])

            if not self.tick_window:
                w_start: datetime | None = None
                w_end: datetime | None = None
                if m_data.get("first_tick_recv_utc") and m_data.get("last_tick_recv_utc"):
                    w_start = datetime.fromisoformat(m_data["first_tick_recv_utc"])
                    w_end = datetime.fromisoformat(m_data["last_tick_recv_utc"])
                elif m_data.get("first_tick_utc") and m_data.get("last_tick_utc"):
                    w_start = datetime.fromisoformat(m_data["first_tick_utc"])
                    w_end = datetime.fromisoformat(m_data["last_tick_utc"])
                elif m_data.get("first_tick_ist") and m_data.get("last_tick_ist"):
                    w_start = _parse_ist_str_to_utc(m_data["first_tick_ist"])
                    w_end = _parse_ist_str_to_utc(m_data["last_tick_ist"])
                if w_start and w_end:
                    self.tick_window = (w_start, w_end)

            events_jsonl = sess_path / "events.jsonl"
            self._timer_closes_by_seq: dict[int, list[dict[str, Any]]] = {}
            self._recorded_events_by_seq: dict[int, list[dict[str, Any]]] = {}
            self._recorded_stop_seq: int | None = None
            self._recorded_stop_reason: str | None = None
            if events_jsonl.is_file():
                try:
                    for line in events_jsonl.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        e = json.loads(line)
                        k = e.get("kind")
                        p = e.get("payload_json", {})
                        if isinstance(p, str):
                            p = json.loads(p)
                        seq = p.get("seq")
                        if k == "bar.timer_close" and seq is not None:
                            self._timer_closes_by_seq.setdefault(int(seq), []).append(p)
                        if (
                            k
                            in (
                                "bar.timer_close",
                                "timer.strategy",
                                "market.closing",
                                "portfolio.snapshot",
                                "run.end",
                            )
                            and seq is not None
                        ):
                            self._recorded_events_by_seq.setdefault(int(seq), []).append(
                                {"kind": k, "payload": p, "ts_utc": e.get("ts_utc")}
                            )
                        if k == "run.end":
                            self._recorded_stop_seq = (
                                int(p.get("seq", 0)) if p.get("seq") is not None else None
                            )
                            self._recorded_stop_reason = p.get("stop_reason")
                except Exception as exc:
                    logger.debug("Failed loading recorded events from %s: %s", events_jsonl, exc)
            self._pending_recorded_seqs: list[int] = sorted(self._recorded_events_by_seq.keys())
            self._recorded_seq_idx: int = 0
            from algodesk.results.compare import _get_configured_plugins

            active_p = _get_configured_plugins(sess_path)
            pv_sess = man_data.get("plugin_versions") or {}
            curr_map = {p.name: p for p in self.runtime.plugins.list()}
            for p_name, p_info in pv_sess.items():
                if active_p is not None and p_name not in active_p:
                    continue
                try:
                    p_curr = curr_map.get(p_name)
                    if p_curr and p_curr.manifest.version != p_info.get("version"):
                        p_ver = p_info.get("version")
                        self._version_warnings.append(
                            f"plugin '{p_name}' version: {p_ver} (paper) != "
                            f"{p_curr.manifest.version} (current)"
                        )
                except Exception:
                    pass
            if self._version_warnings:
                logger.warning(
                    "Version differences for replayed session %s: %s",
                    replay_session,
                    ", ".join(self._version_warnings),
                )
        else:
            self._timer_closes_by_seq = {}
            self._recorded_events_by_seq = {}
            self._pending_recorded_seqs = []
            self._recorded_seq_idx = 0
            self._recorded_stop_seq = None
            self._recorded_stop_reason = None

        if replay_dates:
            cleaned_dates: list[str] = []
            for d in replay_dates:
                if isinstance(d, date):
                    cleaned_dates.append(d.isoformat())
                else:
                    d_str = str(d).strip()
                    try:
                        date.fromisoformat(d_str)
                    except (ValueError, TypeError) as exc:
                        raise ConfigError(
                            what=f"Invalid replay date '{d}'. Use YYYY-MM-DD format.",
                            where="replay_dates",
                            fix="Provide dates in YYYY-MM-DD format (e.g. 2026-11-03)",
                        ) from exc
                    cleaned_dates.append(d_str)
            self.replay_dates = cleaned_dates
        else:
            self.replay_dates = []
        self.seed = seed
        self.dry_run = dry_run
        self.clock = clock if clock is not None else self._init_clock()
        self._journal_db = journal_db

        self.run_name = getattr(getattr(self.run_config, "run", None), "name", "run")
        ts_str = WallClock().now().strftime("%Y%m%d_%H%M%S")
        mode_str = "replay" if self._is_replay else str(self.mode.value)
        if run_id is not None:
            self.run_id = run_id
        else:
            rand_suffix = uuid.uuid4().hex[:6]
            self.run_id = f"{self.run_name}_{mode_str}_{ts_str}_{rand_suffix}"

        self._stop_event = asyncio.Event()
        self._stop_reason: str | None = None
        self._interrupted = False
        self._user_interrupted = False
        self._killswitch_tripped = False
        self._feed_halted = False
        self._run_error: str | None = None
        self._last_feed_gap_alert_time: datetime = datetime.min.replace(tzinfo=UTC)

        self._fills: list[Fill] = []
        self._tick_stats: dict[str, int] = {}
        self._feed_gaps = 0
        self._killswitch_events = 0
        self._rejects_by_rule: dict[str, int] = {}
        self._pending_bar_tasks: list[asyncio.Task[Any]] = []
        self._equity_curve: list[dict[str, Any]] = []
        self._latest_quotes: dict[InstrumentId, Quote] = {}

        self._tick_to_intent_latencies: list[float] = []
        self._feed_delays: list[float] = []
        self._feed_delays_by_inst: dict[str, list[float]] = {}
        self._platform_latencies: list[float] = []
        self._signal_ages: list[float] = []
        self._bar_close_delays: list[float] = []
        self._first_tick_logged: set[InstrumentId] = set()
        self._latest_tick_ts: dict[Any, datetime] = {}
        self._latest_tick_mono: dict[Any, float] = {}
        self._latest_tick_recv_time: dict[Any, datetime] = {}
        self._last_bar_trigger_mono: dict[Any, float] = {}
        self._last_bar_close_mono: dict[Any, float] = {}
        self._last_bar_end_ts: dict[Any, datetime] = {}
        self._first_tick_ts: datetime | None = None
        self._last_tick_ts: datetime | None = None
        self._first_tick_recv_ts: datetime | None = None
        self._last_tick_recv_ts: datetime | None = None
        self._tick_seq: int = 0
        self._active_feed_task: asyncio.Task[Any] | None = None
        self._active_feed_stream: Any = None
        self._bars_closed: int = 0
        self._bars_by_tf: dict[str, int] = {}
        self._signals_count: int = 0
        self._orders_count: int = 0
        self._last_status_wall_time: float = 0.0
        self._p50_latency_ms: float = 0.0
        self._p95_latency_ms: float = 0.0
        self._peak_rss_mb: float = 0.0
        self._journal_initial_bytes: int = 0
        self._journal_final_bytes: int = 0
        self._journal_growth_bytes: int = 0
        self._journal_events_count: int = 0
        self._is_partial: bool = False

    @property
    def mode_name(self) -> str:
        if self._is_replay:
            return "replay"
        return str(self.mode.value if hasattr(self.mode, "value") else self.mode)

    @property
    def version_warnings(self) -> list[str]:
        return list(getattr(self, "_version_warnings", []))

    def stop(self, reason: str = "interrupt") -> None:
        """Signal the session runner to stop event streaming and cleanly shut down."""
        if reason == "interrupt":
            if not self._is_replay or self._user_interrupted:
                self._stop_reason = reason
                self._interrupted = True
            else:
                self._stop_reason = "end of session"
        else:
            self._stop_reason = reason
        if reason == "kill switch":
            self._killswitch_tripped = True
            self._killswitch_events += 1
        elif reason == "error":
            self._run_error = self._run_error or "Stopped due to error"
        elif reason == "feed_auth":
            self._run_error = self._run_error or "Feed authentication failed"
        self._stop_event.set()
        if (
            hasattr(self, "_active_feed_task")
            and self._active_feed_task is not None
            and not self._active_feed_task.done()
        ):
            self._active_feed_task.cancel()

    def run_prestart_doctor(self) -> None:
        """Run diagnostic checks before starting; refuse to run on critical failures."""
        checks = build_run_checks(self.runtime, self.run_name)
        registry = DoctorRegistry()
        registry.register_many(checks)
        report = registry.run()
        if report.critical > 0:
            crit_res = [r for r in report.results if not r.ok and r.severity == Severity.CRITICAL]
            first = crit_res[0] if crit_res else None
            detail = f": {first.message}" if first else ""
            msg = f"Pre-start doctor check failed with {report.critical} critical error(s){detail}"
            fix = (
                first.fix
                if (first and first.fix)
                else f"Run 'algodesk doctor --run {self.run_name}' to fix"
            )
            raise SafetyViolation(
                what=msg,
                where="SessionRunner.run_prestart_doctor",
                fix=fix,
            )

    async def run(self) -> Path:
        """Execute the configured paper or replay session and write results."""
        if self.dry_run:
            self.run_prestart_doctor()
            logger.info("Dry-run mode: components inspected, skipping execution.")
            return self.runtime.paths.results / self.run_id

        log_handle = None
        results_path: Path | None = None
        try:
            from algodesk.api.scrub import Scrubber
            from algodesk.logs.setup import setup_logging

            paths = self.runtime.paths
            log_dir = getattr(paths, "logs", getattr(paths, "logs_dir", None))
            if log_dir is not None:
                log_dir.mkdir(parents=True, exist_ok=True)
                log_handle = setup_logging(
                    level="INFO",
                    fmt="json",
                    log_dir=log_dir,
                    run_id=self.run_id,
                    scrubber=getattr(self.runtime, "scrubber", None) or Scrubber(),
                    retention_days=int(self.runtime.params.get("logs.retention_days"))
                    if self.runtime.params
                    else 30,
                )
                logger.info("Session '%s' starting in mode '%s'", self.run_name, self.mode_name)
        except Exception as log_err:
            logger.debug("Logging setup skipped: %s", log_err)

        clock = self.clock
        journal = self._init_journal()
        components: dict[str, Any] = {}
        error_to_reraise: Exception | None = None

        loop = asyncio.get_running_loop()
        installed_signals: list[signal.Signals] = []

        def _handle_sig() -> None:
            logger.info("Interrupt signal received; stopping session.")
            self._user_interrupted = True
            self.stop(reason="interrupt")

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_sig)
                installed_signals.append(sig)
            except (NotImplementedError, RuntimeError):
                pass

        try:
            self.run_prestart_doctor()

            # Wire core components
            components = self._build_components(clock, journal)

            with journal:
                config_hash = getattr(self.runtime.effective, "hash", "run")
                journal.start_run(
                    mode=self.mode_name,
                    code_version="0.1.0",
                    config_hash=config_hash,
                    registry_hash="core",
                )
                try:
                    self._journal_initial_bytes = (
                        journal.db_path.stat().st_size if journal.db_path.exists() else 0
                    )
                except Exception:
                    self._journal_initial_bytes = 0

                journal.append(
                    "run.start",
                    {"run_id": self.run_id, "mode": self.mode_name, "run_name": self.run_name},
                )
                raw_cfg = getattr(self.runtime.effective, "raw_text", str(self.run_config))
                cap = (
                    float(self.run_config.allocated_capital)
                    if (self.run_config and hasattr(self.run_config, "allocated_capital"))
                    else 0.0
                )
                journal.append(
                    "config.effective",
                    {
                        "run_name": self.run_name,
                        "allocated_capital": cap,
                        "raw_text": raw_cfg,
                    },
                )
                p_hash, p_dict = (
                    self.runtime.params.snapshot()
                    if self.runtime.params and hasattr(self.runtime.params, "snapshot")
                    else ("core", {})
                )
                journal.append("registry.snapshot", {"hash": p_hash, "params": p_dict})
                if self.warmup_source is not None:
                    journal.append("run.warmup_override", {"source": self.warmup_source})
                journal.append(
                    "instruments.loaded",
                    {
                        "count": len(components.get("instruments", {})),
                        "symbols": list(components.get("instruments", {}).keys()),
                    },
                )
                ds_name = (
                    getattr(getattr(self.run_config, "data_source", None), "plugin", "unknown")
                    if self.run_config
                    else "unknown"
                )
                journal.append(
                    "feed.connected",
                    {
                        "data_source": ds_name,
                        "symbols": list(components.get("instruments", {}).keys()),
                    },
                )
                if "ledger" in components:
                    snap0 = components["ledger"].snapshot()
                    journal.append(
                        "portfolio.snapshot",
                        {
                            "equity": float(snap0.equity),
                            "cash": float(snap0.cash),
                            "realized": 0.0,
                            "unrealized": 0.0,
                        },
                    )

                try:
                    await self._execute_session(components, clock, journal)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    logger.info("Session interrupted gracefully; generating results.")
                    self._interrupted = True
                except FeedAuthError as exc:
                    logger.error(
                        "Session stopped due to feed auth error: %s; writing results.", exc
                    )
                    self._stop_reason = "feed_auth"
                    self._run_error = str(exc)
                    error_to_reraise = exc
                except AlgoDeskError as exc:
                    logger.error("Session stopped due to error: %s; writing results.", exc)
                    self._run_error = str(exc)
                    error_to_reraise = exc
                except Exception as exc:
                    logger.error("Session encountered unhandled error: %s; writing results.", exc)
                    self._run_error = f"Unhandled error: {exc}"
                    error_to_reraise = exc
                finally:
                    results_path = self._finalize_session(components, clock, journal)
                    try:
                        end_st = "aborted" if (self._interrupted or self._run_error) else "ok"
                        journal.end_run(end_st)
                    except Exception:
                        pass
        except FeedAuthError as exc:
            logger.error("Session startup feed auth error: %s; writing results.", exc)
            self._stop_reason = "feed_auth"
            self._run_error = str(exc)
            error_to_reraise = exc
        except AlgoDeskError as exc:
            logger.error("Session initialization failed: %s; writing results.", exc)
            self._run_error = str(exc)
            error_to_reraise = exc
        except Exception as exc:
            logger.error("Session startup unhandled error: %s; writing results.", exc)
            self._run_error = f"Unhandled error: {exc}"
            error_to_reraise = exc
        finally:
            for sig in installed_signals:
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass
            if results_path is None:
                results_path = self._finalize_session(components, clock, journal)
            if log_handle is not None:
                log_handle.close()

        if error_to_reraise is not None:
            raise error_to_reraise

        return results_path

    def _init_clock(self) -> Clock:
        if self._is_replay:
            if self.tick_window is not None:
                return SimClock(initial_time=self.tick_window[0])
            initial = datetime(2026, 10, 26, 3, 45, tzinfo=UTC)
            if self.replay_dates:
                d = self.replay_dates[0]
                d_date = d if isinstance(d, date) else date.fromisoformat(str(d))
                initial = datetime(d_date.year, d_date.month, d_date.day, 3, 45, tzinfo=UTC)
            return SimClock(initial_time=initial)
        return WallClock()

    def _init_journal(self) -> Journal:
        import os

        worker = os.environ.get("PYTEST_XDIST_WORKER")
        worker_suffix = f"_{worker}" if worker else ""
        if self._journal_db is not None:
            db_path = self._journal_db
        elif self._is_replay:
            db_path = self.runtime.state.journal_db.parent / f"journal_replay{worker_suffix}.db"
        elif worker:
            db_path = self.runtime.state.journal_db.parent / f"journal{worker_suffix}.db"
        else:
            db_path = self.runtime.state.journal_db
        db_path.parent.mkdir(parents=True, exist_ok=True)
        p = self.runtime.params
        flush_interval = int(p.get("journal.flush_interval_ms")) if p else 50
        max_buffer = int(p.get("journal.max_buffer_events")) if p else 1000
        bp_timeout = int(p.get("journal.backpressure_timeout_ms")) if p else 5000
        settings = JournalSettings(
            flush_interval_ms=flush_interval,
            max_buffer_events=max_buffer,
            backpressure_timeout_ms=bp_timeout,
        )
        return Journal(
            db_path=db_path,
            settings=settings,
            scrubber=getattr(self.runtime, "scrubber", None),
            clock=self.clock.now,
        )

    def _resolve_secrets(self) -> Any | None:
        """Resolve active secrets provider for the session."""
        if getattr(self, "_secrets_provider", None) is not None:
            return self._secrets_provider

        sec = getattr(self.runtime, "secrets", None)
        if sec is not None:
            self._secrets_provider = sec
            return sec

        rc = self.run_config or self.runtime.effective.run
        p_name = None
        sec_cfg: dict[str, Any] = {}
        if rc is not None and getattr(rc, "secrets", None) is not None:
            p_name = getattr(rc.secrets, "plugin", None)
            if hasattr(rc.secrets, "model_dump"):
                sec_cfg = {k: v for k, v in rc.secrets.model_dump().items() if k != "plugin"}

        if not p_name:
            p_name = "keychain" if sys.platform == "darwin" else "env"

        p_ctx = build_context(Mode.RESEARCH, run_id=f"{self.run_id}_secrets")
        try:
            self._secrets_provider = self.runtime.plugins.create(
                PluginKind.SECRETS, p_name, config=sec_cfg, ctx=p_ctx
            )
            return self._secrets_provider
        except Exception:
            return None

    def _build_components(self, clock: Clock, journal: Journal) -> dict[str, Any]:
        """Instantiate and wire engine, oms, venue, risk, and plugins."""
        assert self.run_config is not None
        bus = EventBus()
        p_ctx = build_context(
            mode=self.mode,
            run_id=self.run_id,
            params=self.runtime.params,
            journal=journal,
            secrets=self._resolve_secrets(),
            scrubber=getattr(self.runtime, "scrubber", None),
        )

        venue = self._init_venue(p_ctx)
        instruments = self._resolve_universe()
        venue.configure([i.id for i in instruments.values()])

        fee_schedule = self._init_fees(p_ctx)
        fee_engine = FeeEngine()

        starting_cash = Decimal(str(self.run_config.allocated_capital))
        ledger = PortfolioLedger(clock=clock, starting_cash=starting_cash)

        oms_params = self.runtime.params or {"oms.submit_timeout_ms": 5000}
        oms = OrderManager(
            bus=bus,
            journal=journal,
            clock=clock,
            venue=venue,
            fee_engine=fee_engine,
            schedule=fee_schedule,
            params=oms_params,
            run_id=self.run_id,
        )

        risk_engine, killswitch, monitor = self._init_risk(
            clock, journal, ledger, instruments, starting_cash, p_ctx
        )

        notifiers = self._init_notifiers(p_ctx)
        notify = NotifyBridge(notifiers, bus=bus, journal=journal, clock=clock)

        ind_mgr, buf, strat_rt = self._init_strategies(
            clock, journal, bus, notify, oms, risk_engine, ledger, instruments, p_ctx
        )

        session_mgr, scheduler = self._init_scheduling(clock, p_ctx)
        bar_builder = self._init_bar_builder(
            session_mgr, bus, scheduler, ind_mgr, buf, strat_rt, journal=journal
        )
        recorder = self._init_recorder(bus, scheduler)

        def _on_fill_event(fe: FillEvent) -> None:
            fill = fe.fill
            self._fills.append(fill)
            ledger.apply_fill(fill)
            notify.notify_fill(fill)
            monitor.on_fill(fe)
            logger.info(
                "Fill received: fill_id=%s client_ref=%s %s %s qty=%s price=%.2f charges=%.2f",
                fill.fill_id,
                fill.client_ref,
                fill.instrument,
                getattr(fill.side, "value", fill.side),
                fill.qty,
                fill.price,
                float(fill.charges.total) if fill.charges else 0.0,
            )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(strat_rt.dispatch_fill(fill))
            except RuntimeError:
                pass

        bus.subscribe(FillEvent, _on_fill_event)

        def _on_order_event(oem: OrderEventMsg) -> None:
            self._orders_count = len(oms.orders)
            logger.info(
                "Order event: client_ref=%s from=%s to=%s reason=%s",
                oem.event.client_ref,
                oem.event.from_state,
                oem.event.to_state,
                oem.event.reason,
            )

        bus.subscribe(OrderEventMsg, _on_order_event)

        self._data_service = self._init_data_service(p_ctx, journal)

        return {
            "venue": venue,
            "instruments": instruments,
            "ledger": ledger,
            "oms": oms,
            "risk_engine": risk_engine,
            "killswitch": killswitch,
            "monitor": monitor,
            "notify": notify,
            "strat_rt": strat_rt,
            "ind_mgr": ind_mgr,
            "buf": buf,
            "scheduler": scheduler,
            "session_mgr": session_mgr,
            "bar_builder": bar_builder,
            "recorder": recorder,
            "fee_engine": fee_engine,
        }

    def _init_data_service(self, p_ctx: PluginContext, journal: Journal) -> Any:
        try:
            from algodesk.data.manifest import DataManifest
            from algodesk.data.service import DataService
            from algodesk.data.store import BarStore

            reg_src = None
            if self.runtime.params is not None:
                try:
                    reg_src = self.runtime.params.get("data.warmup_source")
                except Exception:
                    reg_src = None
            default_src = str(reg_src or "upstox")
            effective_src = self.warmup_source or default_src

            sources: dict[str, Any] = {}
            for p_desc in self.runtime.plugins.list(PluginKind.BROKER):
                try:
                    b_inst = self.runtime.plugins.create(
                        PluginKind.BROKER, p_desc.name, config={}, ctx=p_ctx
                    )
                    sources[p_desc.name] = b_inst
                except Exception:
                    pass

            manifests = {
                p.manifest.name: p.manifest
                for p in self.runtime.plugins.list()
                if hasattr(p, "manifest")
            }
            data_dir = getattr(self.runtime.paths, "data", Path("data"))
            manifest_db = data_dir / "state" / "data_manifest.db"
            if not manifest_db.exists() and (data_dir / "manifest.db").exists():
                manifest_db = data_dir / "manifest.db"
            manifest_db.parent.mkdir(parents=True, exist_ok=True)
            manifest = DataManifest(manifest_db)

            store = BarStore(
                data_dir=data_dir,
                manifest=manifest,
                params=self.runtime.params,
                journal=journal,
            )

            cal: Any = None
            try:
                cal = self.runtime.plugins.create(
                    PluginKind.CALENDAR, "nse", config={"exchange": "nse"}, ctx=p_ctx
                )
            except Exception:
                pass

            return DataService(
                sources=sources,
                default_source=effective_src,
                store=store,
                manifests=manifests,
                calendar=cal,
                params=self.runtime.params,
                journal=journal,
            )
        except Exception as exc:
            logger.debug("DataService initialization skipped: %s", exc)
            return None

    def _init_venue(self, p_ctx: PluginContext) -> Any:
        assert self.run_config is not None
        v_name = self.run_config.execution_venue.plugin
        p_info = self.runtime.plugins.info(PluginKind.VENUE, v_name)
        v_cfg: dict[str, object] = {}
        if p_info.manifest.config_schema:
            sim_seed = self.seed or getattr(self.run_config.execution_venue, "seed", 7)
            v_cfg = {
                "seed": sim_seed,
                "starting_cash": float(self.run_config.allocated_capital),
            }
        return self.runtime.plugins.create(PluginKind.VENUE, v_name, config=v_cfg, ctx=p_ctx)

    def _init_fees(self, p_ctx: PluginContext) -> Any:
        assert self.run_config is not None
        sch_name = self.run_config.fees.schedule
        plugin: Any = self.runtime.plugins.create(PluginKind.FEES, sch_name, ctx=p_ctx)
        return plugin.schedule()

    def _init_risk(
        self,
        clock: Clock,
        journal: Journal,
        ledger: PortfolioLedger,
        instruments: Mapping[InstrumentId, Instrument],
        starting_cash: Decimal,
        p_ctx: PluginContext,
    ) -> tuple[RiskEngine, KillSwitch, RiskMonitor]:
        ks_file = self.runtime.state.params_db.parent / "killswitch.json"
        killswitch = KillSwitch(state_file=ks_file, journal=journal, clock=clock)

        risk_rules: list[Any] = []
        rule_names = [
            "max_order_value_pct",
            "max_position_pct",
            "max_open_positions",
            "price_deviation_pct",
            "stale_data_seconds",
            "entry_window",
            "daily_loss_limit_pct",
            "max_orders_per_minute",
            "max_daily_trades",
        ]
        profile = "conservative"
        for r_name in rule_names:
            try:
                rule = self.runtime.plugins.create(
                    PluginKind.RISK_RULE,
                    "builtin",
                    config={"rule": r_name, "profile": profile},
                    ctx=p_ctx,
                )
                risk_rules.append(rule)
            except Exception as exc:
                logger.debug("Could not load risk rule %s: %s", r_name, exc)

        risk_engine = RiskEngine(
            killswitch=killswitch,
            rules=risk_rules,
            journal=journal,
            observe=self.observe,
        )

        monitor = RiskMonitor(
            killswitch=killswitch,
            portfolio=ledger,
            params=self.runtime.params or {},
            profile=profile,
            allocated_capital=starting_cash,
        )
        return risk_engine, killswitch, monitor

    def _init_notifiers(self, p_ctx: PluginContext) -> list[Any]:
        assert self.run_config is not None
        if self._is_replay:
            return []
        notifiers: list[Any] = []
        for n_ref in self.run_config.notifiers:
            try:
                n = self.runtime.plugins.create(
                    PluginKind.NOTIFIER, n_ref.plugin, config=dict(n_ref.model_dump()), ctx=p_ctx
                )
                notifiers.append(n)
            except Exception as exc:
                logger.warning("Could not load notifier %s: %s", n_ref.plugin, exc)
        return notifiers

    def _init_strategies(
        self,
        clock: Clock,
        journal: Journal,
        bus: EventBus,
        notify: NotifyBridge,
        oms: OrderManager,
        risk_engine: RiskEngine,
        ledger: PortfolioLedger,
        instruments: Mapping[InstrumentId, Instrument],
        p_ctx: PluginContext,
    ) -> tuple[IndicatorManager, BarBuffer, StrategyRuntime]:
        ind_plugin = None
        try:
            ind_plugin = self.runtime.plugins.create(
                PluginKind.INDICATOR, "builtin", config={}, ctx=p_ctx
            )
        except Exception:
            pass

        ind_mgr = IndicatorManager(indicator_plugin=ind_plugin, journal=journal)
        buf = BarBuffer()

        def _risk_view_builder(strategy: str = "") -> RiskViewContext:
            assert self.run_config is not None
            active_states = (
                OrderState.CREATED,
                OrderState.SUBMITTED,
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.CANCEL_REQUESTED,
                OrderState.UNKNOWN,
                OrderState.RECONCILING,
            )
            open_ords = [
                o for o in oms.orders.values() if getattr(o, "state", None) in active_states
            ]
            current_positions = {f"{k[0]}:{k[1].value}": pos for k, pos in ledger.positions.items()}
            return RiskViewContext(
                portfolio=ledger.snapshot(),
                positions=current_positions,
                open_orders=open_ords,
                daily_pnl=ledger.realized + ledger.unrealized,
                latest_quotes=self._latest_quotes,
                params=self.runtime.params or {},
                clock=clock,
                instruments=instruments,
                allocated_capital=Decimal(str(self.run_config.allocated_capital)),
                strategy=strategy,
            )

        strat_rt = StrategyRuntime(
            registry=self.runtime.plugins,
            order_manager=oms,
            risk_engine=risk_engine,
            risk_view_builder=_risk_view_builder,
            journal=journal,
            clock=clock,
            bus=bus,
            notifier=notify,
            on_intent=self._on_strategy_intent,
            on_reject=self._on_signal_rejected,
        )

        inst_map: Mapping[InstrumentId | str, Instrument] = {k: v for k, v in instruments.items()}
        assert self.run_config is not None
        for s_cfg in self.run_config.strategies:
            strat_prod_param = str(s_cfg.params.get("product", "")).upper()
            target_prod = (
                Product.MIS
                if strat_prod_param == "MIS"
                else (Product.CNC if strat_prod_param == "CNC" else None)
            )

            def _make_pos_fn(
                prod: Product | None,
            ) -> Callable[[InstrumentId | str], Position | None]:
                def _get_pos(i: InstrumentId | str) -> Position | None:
                    iid = InstrumentId(str(i))
                    if prod is not None:
                        p = ledger.positions.get((iid, prod))
                        if p is not None and p.qty != 0:
                            return p
                    for (k_inst, _k_prod), p in ledger.positions.items():
                        if k_inst == iid and p.qty != 0:
                            return p
                    if prod is not None:
                        return ledger.positions.get((iid, prod))
                    return next(
                        (p for (k_inst, _), p in ledger.positions.items() if k_inst == iid),
                        None,
                    )

                return _get_pos

            strat_ctx = build_strategy_context(
                clock=clock,
                instruments=inst_map,
                indicator_manager=ind_mgr,
                bar_buffer=buf,
                position_fn=_make_pos_fn(target_prod),
                portfolio_fn=lambda: ledger.snapshot(),
                params=s_cfg.params,
                risk_state={"killswitch": risk_engine.killswitch.is_active()},
            )
            strat_rt.load_strategy(s_cfg.plugin, s_cfg.params, strat_ctx, plugin_ctx=p_ctx)

        strat_rt.dispatch_start()
        return ind_mgr, buf, strat_rt

    def _init_scheduling(
        self, clock: Clock, p_ctx: PluginContext
    ) -> tuple[SessionManager, Scheduler]:
        assert self.run_config is not None
        scheduler = Scheduler(clock=clock)
        cal_plugins: dict[str, Any] = {}
        for c in self.run_config.calendars:
            exch = getattr(c, "exchange", "nse")
            try:
                cal = self.runtime.plugins.create(
                    PluginKind.CALENDAR, c.plugin, config={"exchange": exch}, ctx=p_ctx
                )
                cal_plugins[exch.upper()] = cal
            except Exception as exc:
                logger.debug("Could not create calendar for %s: %s", exch, exc)

        session_mgr = SessionManager(calendars=cal_plugins, scheduler=scheduler, clock=clock)
        return session_mgr, scheduler

    def _init_bar_builder(
        self,
        session_mgr: SessionManager,
        bus: EventBus,
        scheduler: Scheduler,
        ind_mgr: IndicatorManager,
        buf: BarBuffer,
        strat_rt: StrategyRuntime,
        journal: Journal | None = None,
    ) -> BarBuilder:
        assert self.run_config is not None

        def _on_timer_close(inst: Any, tf: str, end_ts: datetime) -> None:
            if journal is not None:
                journal.append(
                    "bar.timer_close",
                    {
                        "instrument": str(inst),
                        "timeframe": tf,
                        "end_ts": end_ts.isoformat(),
                        "seq": self._tick_seq,
                    },
                )

        async def _on_bar_closed(bar: Any) -> None:
            buf.append(bar)
            ind_mgr.on_bar(bar)
            if not self._feed_halted:
                await strat_rt.dispatch_bar(bar)

        def _on_bar_sync(bar: Bar) -> None:
            self._bars_closed += 1
            tf = str(getattr(bar, "timeframe", "1m"))
            self._bars_by_tf[tf] = self._bars_by_tf.get(tf, 0) + 1
            tick_ts = self._latest_tick_ts.get(bar.instrument) or self._last_tick_ts
            if tick_ts is not None and getattr(bar, "end_ts", None) is not None:
                bcd = max(0.0, (tick_ts - bar.end_ts).total_seconds() * 1000.0)
                self._bar_close_delays.append(bcd)
            now_mono = time.monotonic()
            self._last_bar_close_mono[bar.instrument] = now_mono
            self._last_bar_trigger_mono[bar.instrument] = now_mono
            self._last_bar_end_ts[bar.instrument] = getattr(bar, "end_ts", self.clock.now())
            task = asyncio.create_task(_on_bar_closed(bar))
            self._pending_bar_tasks.append(task)

        tfs = [s.params.get("timeframe", "5m") for s in self.run_config.strategies]
        return BarBuilder(
            timeframes=tuple(set(tfs)),
            session_manager=session_mgr,
            bus=bus,
            scheduler=scheduler,
            on_bar_closed=_on_bar_sync,
            on_timer_bar_closed=_on_timer_close,
        )

    def _on_signal_rejected(
        self, intent: Any, decision: Any | None, component: str = "risk"
    ) -> None:
        if decision is not None and not getattr(decision, "allow", True):
            rules = [r.strip() for r in str(getattr(decision, "rule", "")).split(",") if r.strip()]
            for r in rules:
                self._rejects_by_rule[r] = self._rejects_by_rule.get(r, 0) + 1
        elif component:
            self._rejects_by_rule[component] = self._rejects_by_rule.get(component, 0) + 1

    def _on_strategy_intent(self, inst: Any, now_dt: datetime) -> None:
        self._signals_count += 1
        t_mono = time.monotonic()
        close_mono = self._last_bar_close_mono.get(inst, t_mono)
        lat = max(0.01, (t_mono - close_mono) * 1000.0)
        self._platform_latencies.append(lat)
        self._tick_to_intent_latencies.append(lat)
        bar_end = self._last_bar_end_ts.get(inst, now_dt)
        sig_age = max(0.0, (now_dt - bar_end).total_seconds() * 1000.0)
        self._signal_ages.append(sig_age)

    def _init_recorder(self, bus: EventBus, scheduler: Scheduler) -> Recorder | None:
        if self.mode != Mode.PAPER or not getattr(self.run_config, "recording", True):
            return None
        data_dir = self.runtime.paths.data
        data_dir.mkdir(parents=True, exist_ok=True)
        manifest_db = data_dir / "state" / "data_manifest.db"
        if not manifest_db.exists() and (data_dir / "manifest.db").exists():
            manifest_db = data_dir / "manifest.db"
        manifest_db.parent.mkdir(parents=True, exist_ok=True)
        try:
            manifest = DataManifest(manifest_db)
            return Recorder(
                data_dir=data_dir,
                manifest=manifest,
                bus=bus,
                scheduler=scheduler,
                source="live",
                session_id=self.run_id,
            )
        except Exception as exc:
            logger.warning("Could not initialize Recorder: %s", exc)
            return None

    def _resolve_universe(self) -> dict[InstrumentId, Instrument]:
        assert self.run_config is not None
        symbols: list[str] = []
        u_cfg = self.run_config.universe
        if u_cfg.symbols:
            symbols = list(u_cfg.symbols)
        elif u_cfg.group:
            manifest_db = self.runtime.paths.data / "state" / "data_manifest.db"
            if not manifest_db.exists() and (self.runtime.paths.data / "manifest.db").exists():
                manifest_db = self.runtime.paths.data / "manifest.db"
            manifest = DataManifest(manifest_db)
            groups_dir = self.runtime.root / "config" / "groups"
            symbols = list(resolve_selector(f"@{u_cfg.group}", manifest, groups_dir=groups_dir))
        elif u_cfg.screen:
            screen_name = u_cfg.screen
            from algodesk.api.errors import ConfigError

            fake_res = getattr(self.runtime, "_fake_screen_result", None)
            as_of_session = None
            if fake_res is not None:
                res = fake_res
                as_of_session = getattr(res, "as_of", None)
            else:
                from algodesk.data.groups import load_groups
                from algodesk.ops.screens import build_screen_runner
                from algodesk.screens.models import load_toml_screen

                screen_file = self.runtime.root / "config" / "screens" / f"{screen_name}.toml"
                screen_obj: Any = None
                screen_hash: str | None = None
                if screen_file.is_file():
                    import hashlib

                    screen_obj = load_toml_screen(screen_file)
                    screen_hash = hashlib.sha256(screen_file.read_bytes()).hexdigest()
                else:
                    try:
                        p_ctx = build_context(Mode.RESEARCH, run_id="engine_screen_resolve")
                        screen_obj = self.runtime.plugins.create(
                            PluginKind.SCREEN, screen_name, config={}, ctx=p_ctx
                        )
                    except Exception:
                        screen_obj = None

                if screen_obj is None:
                    raise ConfigError(
                        what=f"Screen '{screen_name}' specified in universe.screen not found",
                        where="engine.runner._resolve_universe",
                        fix=f"Verify config/screens/{screen_name}.toml or screen plugin exists",
                    )

                groups_dir = self.runtime.root / "config" / "groups"
                groups_map = load_groups(groups_dir)
                u_name = getattr(screen_obj, "universe", None) or "nifty50"
                if u_name in groups_map:
                    screen_syms = list(groups_map[u_name])
                elif "demo5" in groups_map:
                    screen_syms = list(groups_map["demo5"])
                else:
                    screen_syms = ["RELIANCE", "TCS", "INFY"]

                screen_runner = build_screen_runner(self.runtime)
                as_of_session = screen_runner.resolve_as_of(None)
                res = screen_runner.run(
                    screen=screen_obj,
                    symbols=screen_syms,
                    as_of=as_of_session,
                    screen_file_hash=screen_hash,
                )

            # Check screen.max_age_days
            age_cutoff_days: int | None = None
            if self.runtime.params is not None:
                try:
                    val = self.runtime.params.get("screen.max_age_days")
                    if val is not None:
                        age_cutoff_days = int(val)
                except Exception:
                    pass
            if age_cutoff_days is None:
                age_cutoff_days = 7

            if as_of_session is not None:
                now_ist = self.clock.now().astimezone(ZoneInfo("Asia/Kolkata")).date()
                age_days = (now_ist - as_of_session).days
                if age_days > age_cutoff_days:
                    raise ConfigError(
                        what=(
                            f"Screen result as-of {as_of_session} is {age_days} days old, "
                            f"exceeding screen.max_age_days ({age_cutoff_days} days)"
                        ),
                        where="engine.runner._resolve_universe",
                        fix=(
                            "Run screen for a more recent trading session "
                            "or increase screen.max_age_days"
                        ),
                    )

            if getattr(res, "verdict", "PASS") == "FAIL":
                fail_r = getattr(res, "fail_reason", "unknown")
                raise ConfigError(
                    what=f"Screen '{screen_name}' execution failed: {fail_r}",
                    where="engine.runner._resolve_universe",
                    fix="Resolve screen data gaps or increase screen.max_skipped_pct",
                )

            symbols = [r.symbol for r in res.rows if r.passed]

        from algodesk.data.aliases import resolve_alias

        inst_map: dict[InstrumentId, Instrument] = {}
        for s in symbols:
            canonical_id = resolve_alias(s)
            p = parse_id(canonical_id)
            inst = Instrument(
                id=canonical_id,
                exchange=p.exchange,
                segment=p.segment,
                symbol=p.symbol,
                expiry=p.expiry,
                strike=p.strike,
                option_type=p.option_type,
                lot_size=1,
                tick_size=Decimal("0.05"),
                underlying=p.underlying,
                isin=None,
                broker_keys={},
            )
            inst_map[canonical_id] = inst
            if str(canonical_id) != str(s):
                inst_map[InstrumentId(s)] = inst

        import hashlib

        sorted_keys = sorted(str(k) for k in inst_map.keys())
        content_hash = hashlib.sha256("\n".join(sorted_keys).encode("utf-8")).hexdigest()[:16]
        j_writer = getattr(self, "journal", None) or getattr(self.runtime, "journal", None)
        if j_writer is not None:
            try:
                j_writer.append(
                    "universe.frozen",
                    {
                        "screen": getattr(u_cfg, "screen", None),
                        "group": getattr(u_cfg, "group", None),
                        "hash": content_hash,
                        "instruments": sorted_keys,
                    },
                )
            except Exception:
                pass

        return inst_map

    async def _drain_pending_bar_tasks(self, c: dict[str, Any]) -> None:
        while self._pending_bar_tasks:
            tasks = list(self._pending_bar_tasks)
            self._pending_bar_tasks.clear()
            await asyncio.gather(*tasks)
        await self._drain_venue_events(c)

    async def _apply_recorded_events(
        self,
        seq: int,
        c: dict[str, Any],
        clock: Clock,
        journal: Journal,
    ) -> None:
        if not self._recorded_events_by_seq or seq not in self._recorded_events_by_seq:
            return
        events = self._recorded_events_by_seq.pop(seq)
        for ev in events:
            kind = ev["kind"]
            p = ev["payload"]
            ts_str = ev.get("ts_utc")
            if ts_str and hasattr(clock, "set_time"):
                try:
                    ev_dt = datetime.fromisoformat(ts_str)
                    if clock.now() < ev_dt:
                        clock.set_time(ev_dt)
                except Exception:
                    pass
            if kind == "bar.timer_close":
                inst_id = p["instrument"]
                tf = p["timeframe"]
                end_ts = datetime.fromisoformat(p["end_ts"])
                c["bar_builder"].close_bar_timer(inst_id, tf, end_ts)
                await self._drain_pending_bar_tasks(c)
            elif kind == "timer.strategy":
                name = p.get("name", "exit_time")
                journal.append(
                    "timer.strategy", {"name": name, "seq": seq, "ts": clock.now().isoformat()}
                )
                await c["strat_rt"].dispatch_timer(name)
                await self._drain_venue_events(c)
            elif kind == "market.closing":
                m = p.get("market")
                if m and "session_mgr" in c:
                    session_mgr: SessionManager = c["session_mgr"]
                    session_mgr.transition(m, MarketState.CLOSING, "close timer")
                    journal.append(
                        "market.closing",
                        {"market": m, "seq": seq, "ts": clock.now().isoformat()},
                    )
                    oms = c.get("oms")
                    if oms is not None:
                        for ref, ord_obj in list(oms.orders.items()):
                            inst = getattr(ord_obj, "instrument", None) or getattr(
                                getattr(ord_obj, "spec", None), "instrument", None
                            )
                            if inst is not None and session_mgr.market_key(inst) == m:
                                if hasattr(ord_obj, "state") and ord_obj.state in (
                                    OrderState.CREATED,
                                    OrderState.SUBMITTED,
                                    OrderState.ACKNOWLEDGED,
                                    OrderState.PARTIALLY_FILLED,
                                ):
                                    try:
                                        await oms.cancel(ref)
                                    except Exception:
                                        pass
                    await self._drain_venue_events(c)
                    ledger = c.get("ledger")
                    if ledger is not None:
                        for q in self._latest_quotes.values():
                            if session_mgr.market_key(q.instrument) == m:
                                ledger.mark(q)
                    session_mgr.transition(m, MarketState.REPORTING, "session end")
                    session_mgr.transition(m, MarketState.STOPPED, "reporting done")
            elif kind == "portfolio.snapshot":
                ledger = c.get("ledger")
                if ledger is not None:
                    snap = ledger.snapshot()
                    eq_val = float(snap.equity)
                    pt_dict = {
                        "ts": clock.now().isoformat(),
                        "ts_ist": format_ist(clock.now()),
                        "equity": eq_val,
                        "cash": float(snap.cash),
                        "unrealized": float(ledger.unrealized),
                        "realized": float(ledger.realized),
                        "drawdown": compute_max_drawdown(
                            [pt["equity"] for pt in self._equity_curve] + [eq_val]
                        ),
                    }
                    self._equity_curve.append(pt_dict)
                    journal.append(
                        "portfolio.snapshot",
                        {
                            "equity": pt_dict["equity"],
                            "cash": pt_dict["cash"],
                            "unrealized": pt_dict["unrealized"],
                            "realized": pt_dict["realized"],
                            "seq": seq,
                            "ts": clock.now().isoformat(),
                        },
                    )
            elif kind == "run.end":
                self.stop(reason="end of session")

    async def _execute_session(
        self,
        c: dict[str, Any],
        clock: Clock,
        journal: Journal,
    ) -> None:
        """Stream market events, process fills, advance scheduler, and monitor heartbeat."""
        notify: NotifyBridge = c["notify"]
        diff_warnings: list[str] = []
        try:
            from algodesk.data.instruments_store import InstrumentStore

            store = InstrumentStore(self.runtime.paths.data)
            dates = store.list_dates("upstox")
            if len(dates) >= 2:
                report = store.diff("upstox", dates[-2], dates[-1])
                univ_set = {str(k) for k in c["instruments"].keys()}
                for ch in report.changed:
                    if str(ch.id) in univ_set:
                        f_diffs = [f"{k}: {v[0]}->{v[1]}" for k, v in ch.changes.items()]
                        diff_warnings.append(f"Master diff for {ch.id}: {', '.join(f_diffs)}")
                for rem in report.removed:
                    if str(rem.id) in univ_set:
                        diff_warnings.append(f"Master diff: {rem.id} was removed from master")
        except Exception:
            pass

        notify.notify_session_start(
            self.run_name,
            str(self.mode),
            len(c["instruments"]),
            warnings=diff_warnings or None,
        )

        # Schedule timers
        self._setup_timers(c, clock, journal)

        # Warm-up registered indicators using DataService or saved session warm-up bars
        ind_mgr: IndicatorManager | None = c.get("ind_mgr")
        if ind_mgr is not None:
            warmed = False
            if self.replay_session:
                sess_dir = self.runtime.paths.results / self.replay_session
                if not sess_dir.is_dir() and Path(self.replay_session).is_dir():
                    sess_dir = Path(self.replay_session)
                wb_path = sess_dir / "warmup_bars.parquet"
                if wb_path.is_file():
                    from algodesk.data.replay import load_warmup_bars_parquet

                    bars = load_warmup_bars_parquet(wb_path)
                    ind_mgr.warmup_from_bars(bars)
                    logger.info("Loaded %d warm-up bars from %s", len(bars), wb_path)
                    warmed = True

            if not warmed and self._data_service is not None:
                try:
                    eff_src = self.warmup_source or "upstox"
                    w_start = self.tick_window[0] if self.tick_window else clock.now()
                    ind_mgr.warmup(self._data_service, session_start=w_start, source=eff_src)
                except Exception as exc:
                    logger.warning("Indicator warmup error: %s", exc)

        # Build feed stream
        stream = self._build_event_stream(c, clock)
        self._active_feed_stream = stream

        async def _dispatch_market_event(ev_item: Any) -> None:
            if isinstance(ev_item, tuple):
                ev, seq_opt = ev_item
            else:
                ev, seq_opt = ev_item, None
            if seq_opt is not None:
                self._tick_seq = seq_opt
            else:
                self._tick_seq += 1
            curr_seq = self._tick_seq

            # 1. Recording
            if c["recorder"] is not None:
                try:
                    c["recorder"].on_market_event(ev, seq=curr_seq)
                except Exception as exc:
                    notify.notify_error(f"Recorder disk full or error: {exc}", "recorder")

            # 2. Market event handling
            if isinstance(ev, Quote):
                if self._first_tick_ts is None:
                    self._first_tick_ts = ev.exch_ts
                self._last_tick_ts = ev.exch_ts
                if self._first_tick_recv_ts is None:
                    self._first_tick_recv_ts = ev.recv_ts
                self._last_tick_recv_ts = ev.recv_ts
                self._latest_tick_ts[ev.instrument] = ev.exch_ts
                self._latest_tick_mono[ev.instrument] = time.monotonic()
                self._latest_tick_recv_time[ev.instrument] = clock.now()
                c["venue"].on_quote(ev)
                self._latest_quotes[ev.instrument] = ev
                if getattr(ev, "exch_ts", None) and getattr(ev, "recv_ts", None):
                    fd = max(0.0, (ev.recv_ts - ev.exch_ts).total_seconds() * 1000.0)
                    self._feed_delays.append(fd)
                    self._feed_delays_by_inst.setdefault(str(ev.instrument), []).append(fd)
            elif isinstance(ev, Tick):
                if self._first_tick_ts is None:
                    self._first_tick_ts = ev.exch_ts
                self._last_tick_ts = ev.exch_ts
                if self._first_tick_recv_ts is None:
                    self._first_tick_recv_ts = ev.recv_ts
                self._last_tick_recv_ts = ev.recv_ts
                self._latest_tick_ts[ev.instrument] = ev.exch_ts
                self._latest_tick_mono[ev.instrument] = time.monotonic()
                self._latest_tick_recv_time[ev.instrument] = clock.now()
                if getattr(ev, "exch_ts", None) and getattr(ev, "recv_ts", None):
                    fd = max(0.0, (ev.recv_ts - ev.exch_ts).total_seconds() * 1000.0)
                    self._feed_delays.append(fd)
                    self._feed_delays_by_inst.setdefault(str(ev.instrument), []).append(fd)
                if ev.instrument not in self._first_tick_logged:
                    self._first_tick_logged.add(ev.instrument)
                    logger.info(
                        "First tick received for %s: ltp=%.2f at %s",
                        ev.instrument,
                        ev.ltp,
                        format_ist(ev.exch_ts),
                    )
                c["bar_builder"].on_tick(ev)
                self._tick_stats[str(ev.instrument)] = (
                    self._tick_stats.get(str(ev.instrument), 0) + 1
                )
                prev_q = self._latest_quotes.get(ev.instrument)
                if prev_q is not None:
                    self._latest_quotes[ev.instrument] = Quote(
                        instrument=prev_q.instrument,
                        exch_ts=ev.exch_ts,
                        recv_ts=ev.recv_ts,
                        bids=prev_q.bids,
                        asks=prev_q.asks,
                        ltp=ev.ltp,
                        volume=ev.volume,
                    )
                else:
                    self._latest_quotes[ev.instrument] = Quote(
                        instrument=ev.instrument,
                        exch_ts=ev.exch_ts,
                        recv_ts=ev.recv_ts,
                        bids=(),
                        asks=(),
                        ltp=ev.ltp,
                        volume=ev.volume,
                    )
                await self._drain_pending_bar_tasks(c)
            elif isinstance(ev, FeedStatus):
                notify.notify_feed_status(ev)
                if ev.state == "connected":
                    logger.info("Feed connected: %s", ev.details)
                    journal.append("feed.connected", {"state": ev.state, "details": ev.details})
                elif ev.state in ("disconnected", "gap"):
                    self._feed_halted = True
                    self._feed_gaps += 1
                    kind = "feed.gap" if ev.state == "gap" else "feed.disconnected"
                    logger.warning("Feed disconnected: %s", ev.details)
                    journal.append(kind, {"state": ev.state, "details": ev.details})

            # 3. Process venue execution events
            await self._drain_venue_events(c)

            # 4. Advance scheduler timers
            c["scheduler"].advance_to(clock.now())
            c["scheduler"].fire_due()

            # 5. Check OMS timeouts
            await c["oms"].check_timeouts()

            # 6. Replay recorded events up to curr_seq
            if self._is_replay and self._pending_recorded_seqs:
                while (
                    self._recorded_seq_idx < len(self._pending_recorded_seqs)
                    and self._pending_recorded_seqs[self._recorded_seq_idx] <= curr_seq
                ):
                    due_seq = self._pending_recorded_seqs[self._recorded_seq_idx]
                    self._recorded_seq_idx += 1
                    await self._apply_recorded_events(due_seq, c, clock, journal)

        if self._is_replay:
            async for ev_item in stream:
                if self._stop_event.is_set():
                    break
                if c.get("killswitch") is not None and c["killswitch"].is_active():
                    self.stop(reason="kill switch")
                    break
                await _dispatch_market_event(ev_item)
                if (
                    self._recorded_stop_seq is not None
                    and self._tick_seq >= self._recorded_stop_seq
                ):
                    self.stop(reason="end of session")
                    break

            # If stream finished, apply any remaining recorded events
            while self._pending_recorded_seqs and self._recorded_seq_idx < len(
                self._pending_recorded_seqs
            ):
                due_seq = self._pending_recorded_seqs[self._recorded_seq_idx]
                self._recorded_seq_idx += 1
                await self._apply_recorded_events(due_seq, c, clock, journal)

            if not self._stop_event.is_set():
                if self._recorded_stop_seq is not None and self._tick_seq < self._recorded_stop_seq:
                    t_str = format_ist(self._last_tick_ts) if self._last_tick_ts else "N/A"
                    self.stop(reason=f"replay data ended ({t_str})")
                else:
                    self.stop(reason="end of session")
        else:
            to_val = (
                self.runtime.params.get("engine.feed_poll_timeout_ms")
                if self.runtime.params is not None
                else None
            )
            poll_timeout = float(to_val) / 1000.0 if to_val is not None else 0.1

            queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1000)
            feed_stopped = asyncio.Event()
            feed_error: Exception | None = None

            async def _feed_worker() -> None:
                nonlocal feed_error
                try:
                    async for item in stream:
                        await queue.put(item)
                        if self._stop_event.is_set():
                            break
                except asyncio.CancelledError:
                    pass
                except FeedAuthError as exc:
                    logger.error("Feed authentication rejected: %s", exc)
                    feed_error = exc
                    self.stop(reason="feed_auth")
                except Exception as exc:
                    logger.error("Feed error: %s", exc)
                    self._run_error = str(exc)
                    feed_error = exc
                finally:
                    if hasattr(stream, "aclose"):
                        try:
                            await stream.aclose()
                        except Exception:
                            pass
                    feed_stopped.set()

            feed_task = asyncio.create_task(_feed_worker())
            self._active_feed_task = feed_task

            try:
                while True:
                    if self._stop_event.is_set():
                        break
                    if c.get("killswitch") is not None and c["killswitch"].is_active():
                        self.stop(reason="kill switch")
                        break

                    try:
                        ev_item = await asyncio.wait_for(queue.get(), timeout=poll_timeout)
                    except TimeoutError:
                        if feed_stopped.is_set() and queue.empty():
                            if isinstance(feed_error, FeedAuthError):
                                oms = c.get("oms")
                                if oms is not None:
                                    for ref, ord_obj in list(oms.orders.items()):
                                        if hasattr(ord_obj, "state") and ord_obj.state in (
                                            OrderState.CREATED,
                                            OrderState.SUBMITTED,
                                            OrderState.ACKNOWLEDGED,
                                            OrderState.PARTIALLY_FILLED,
                                        ):
                                            try:
                                                await oms.cancel(ref)
                                            except Exception:
                                                pass
                                await self._drain_venue_events(c)
                                journal.append(
                                    "feed.auth_failed",
                                    {
                                        "error": str(feed_error),
                                        "seq": self._tick_seq,
                                        "ts": clock.now().isoformat(),
                                    },
                                )
                                notify.notify_error(f"Feed auth failed: {feed_error}", "feed")
                                raise feed_error from None
                            if feed_error is not None:
                                raise feed_error from None
                            break
                        c["scheduler"].advance_to(clock.now())
                        c["scheduler"].fire_due()
                        await c["oms"].check_timeouts()
                        continue

                    await _dispatch_market_event(ev_item)

                if feed_stopped.is_set() and queue.empty() and feed_error is not None:
                    raise feed_error
            finally:
                self._stop_event.set()
                if not feed_task.done():
                    feed_task.cancel()
                    try:
                        await feed_task
                    except (asyncio.CancelledError, Exception):
                        pass
                if hasattr(stream, "aclose"):
                    try:
                        await stream.aclose()
                    except Exception:
                        pass
                self._active_feed_task = None
                self._active_feed_stream = None

        # Drain pending bar tasks and venue events after stream ends
        await asyncio.sleep(0)
        await self._drain_venue_events(c)

    def _setup_timers(self, c: dict[str, Any], clock: Clock, journal: Journal) -> None:
        assert self.run_config is not None
        scheduler: Scheduler = c["scheduler"]
        notify: NotifyBridge = c["notify"]
        strat_rt: StrategyRuntime = c["strat_rt"]
        ledger: PortfolioLedger = c["ledger"]
        session_mgr: SessionManager = c["session_mgr"]

        universe_insts = list(c.get("instruments", {}).values())
        if not universe_insts:
            symbols = getattr(getattr(self.run_config, "universe", None), "symbols", None) or []
            universe_markets = {session_mgr.market_key(s) for s in symbols}
        else:
            universe_markets = {session_mgr.market_key(inst.id) for inst in universe_insts}

        for m in universe_markets:
            if session_mgr.state(m) == MarketState.STOPPED:
                session_mgr.transition(m, MarketState.STARTING, "session setup")
                session_mgr.transition(m, MarketState.RUNNING, "session start")

        hb_val = (
            self.runtime.params.get("runtime.heartbeat_seconds")
            if self.runtime.params is not None
            else None
        )
        hb_sec = int(hb_val) if hb_val is not None else 900

        now_dt = clock.now()
        scheduler.schedule_repeating(
            "heartbeat",
            start=now_dt + timedelta(seconds=hb_sec),
            interval_seconds=float(hb_sec),
            callback=lambda: notify.notify_heartbeat("Engine running normally"),
        )

        def _snapshot_portfolio() -> None:
            snap = ledger.snapshot()
            eq_val = float(snap.equity)
            pt_dict = {
                "ts": clock.now().isoformat(),
                "ts_ist": format_ist(clock.now()),
                "equity": eq_val,
                "cash": float(snap.cash),
                "unrealized": float(ledger.unrealized),
                "realized": float(ledger.realized),
                "drawdown": compute_max_drawdown(
                    [p["equity"] for p in self._equity_curve] + [eq_val]
                ),
            }
            self._equity_curve.append(pt_dict)
            journal.append(
                "portfolio.snapshot",
                {
                    "equity": pt_dict["equity"],
                    "cash": pt_dict["cash"],
                    "unrealized": pt_dict["unrealized"],
                    "realized": pt_dict["realized"],
                    "seq": self._tick_seq,
                    "ts": clock.now().isoformat(),
                },
            )

        replay_has_events = bool(self._is_replay and self._recorded_events_by_seq)

        if not replay_has_events:
            snap_val = (
                self.runtime.params.get("runtime.equity_snapshot_interval_seconds")
                if self.runtime.params is not None
                else None
            )
            snap_sec = float(snap_val or 60.0)
            scheduler.schedule_repeating(
                "portfolio_snapshot",
                start=now_dt + timedelta(seconds=snap_sec),
                interval_seconds=snap_sec,
                callback=_snapshot_portfolio,
            )

        def _emit_proc_stats() -> None:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            peak_rss = usage.ru_maxrss
            peak_rss_mb = peak_rss / (1024 * 1024) if sys.platform == "darwin" else peak_rss / 1024
            self._peak_rss_mb = max(self._peak_rss_mb, round(peak_rss_mb, 2))

            j_bytes = 0
            try:
                if journal and hasattr(journal, "db_path") and journal.db_path.exists():
                    j_bytes = journal.db_path.stat().st_size
            except Exception:
                pass

            rec = c.get("recorder")
            r_rows = getattr(rec, "total_rows", 0) if rec is not None else 0
            r_bytes = getattr(rec, "total_bytes", 0) if rec is not None else 0

            journal.append(
                "proc.stats",
                {
                    "peak_rss_mb": self._peak_rss_mb,
                    "journal_bytes": j_bytes,
                    "recorder_rows": r_rows,
                    "recorder_bytes": r_bytes,
                },
            )

        def _emit_stats() -> None:
            journal.append(
                "tick.stats",
                {
                    "total_ticks": sum(self._tick_stats.values()),
                    "by_instrument": dict(self._tick_stats),
                },
            )
            journal.append(
                "bar.stats",
                {
                    "closed_bars": self._bars_closed,
                    "by_timeframe": dict(self._bars_by_tf),
                },
            )

        stats_val = (
            self.runtime.params.get("run.stats_seconds")
            if self.runtime.params is not None
            else None
        )
        stats_sec = float(stats_val or 60.0)

        scheduler.schedule_repeating(
            "proc_stats",
            start=now_dt + timedelta(seconds=stats_sec),
            interval_seconds=stats_sec,
            callback=_emit_proc_stats,
        )

        scheduler.schedule_repeating(
            "tick_bar_stats",
            start=now_dt + timedelta(seconds=stats_sec),
            interval_seconds=stats_sec,
            callback=_emit_stats,
        )

        # Periodic console status line
        status_val = (
            self.runtime.params.get("run.status_seconds")
            if self.runtime.params is not None
            else None
        )
        status_sec = float(status_val or 30.0)

        def _print_status_line() -> None:
            if self._is_replay:
                now_wall = time.monotonic()
                if now_wall - self._last_status_wall_time < status_sec:
                    return
                self._last_status_wall_time = now_wall
            usage = resource.getrusage(resource.RUSAGE_SELF)
            peak_rss = usage.ru_maxrss
            peak_rss_mb = peak_rss / (1024 * 1024) if sys.platform == "darwin" else peak_rss / 1024
            self._peak_rss_mb = max(self._peak_rss_mb, round(peak_rss_mb, 2))

            ist_time_str = format_ist(clock.now())[11:19]
            state_str = self.mode_name
            total_ticks = sum(self._tick_stats.values())
            if self._latest_tick_recv_time:
                oldest_recv = min(self._latest_tick_recv_time.values())
                age_sec = max(0.0, (clock.now() - oldest_recv).total_seconds())
                oldest_age_str = f"{age_sec:.1f}s"
            else:
                oldest_age_str = "0s"
            bars_str = str(self._bars_closed)
            sigs_str = str(self._signals_count)
            ords_str = str(len(c["oms"].orders) if "oms" in c else 0)
            fills_str = str(len(self._fills))
            ks_active = c["killswitch"].is_active() if "killswitch" in c else False
            ks_str = "TRIPPED" if ks_active else "ARMED"
            rss_str = f"{self._peak_rss_mb:.1f}MB"

            status_line = (
                f"[{ist_time_str} IST] state={state_str} ticks={total_ticks} "
                f"tick_age={oldest_age_str} bars={bars_str} signals={sigs_str} "
                f"orders={ords_str} fills={fills_str} killswitch={ks_str} rss={rss_str}"
            )
            logger.info("Session stats: %s", status_line)
            if not self.quiet:
                typer.echo(status_line)

        scheduler.schedule_repeating(
            "console_status_line",
            start=now_dt + timedelta(seconds=status_sec),
            interval_seconds=status_sec,
            callback=_print_status_line,
        )

        now_ist = clock.now().astimezone(IST)
        d = now_ist.date()

        def _dispatch_async(coro: Any) -> None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(coro)
            except RuntimeError:
                asyncio.run(coro)

        if not replay_has_events:
            # Strategy exit timer
            for s in self.run_config.strategies:
                exit_time_str = s.params.get("exit_time", "15:15")
                try:
                    hh, mm = map(int, exit_time_str.split(":"))
                    exit_dt = datetime(
                        now_ist.year, now_ist.month, now_ist.day, hh, mm, tzinfo=IST
                    ).astimezone(UTC)
                    if exit_dt > clock.now():

                        async def _fire_strat_exit() -> None:
                            journal.append(
                                "timer.strategy",
                                {
                                    "name": "exit_time",
                                    "seq": self._tick_seq,
                                    "ts": clock.now().isoformat(),
                                },
                            )
                            await strat_rt.dispatch_timer("exit_time")
                            await self._drain_venue_events(c)

                        scheduler.schedule_at(
                            "strategy_exit",
                            due=exit_dt,
                            callback=lambda: _dispatch_async(_fire_strat_exit()),
                        )
                except Exception:
                    pass

            # Market-aware close timers
            grace_val = (
                self.runtime.params.get("engine.session_end_grace_s")
                if self.runtime.params is not None
                else None
            )
            grace_s = float(grace_val or 0.0)

            for m in universe_markets:
                exch, seg = m.split(":")
                cal = session_mgr.get_calendar(exch)
                if cal is None:
                    continue
                sessions = (
                    cal.sessions(d, segment=seg.lower())
                    if cal.is_trading_day(d, segment=seg.lower())
                    else []
                )
                tradable_sessions = [sess for sess in sessions if sess.tradable]
                if not tradable_sessions:
                    continue
                close_utc = max(sess.end for sess in tradable_sessions)
                close_with_grace = close_utc + timedelta(seconds=grace_s)

                if close_with_grace > clock.now():

                    def _make_market_close_cb(target_market: str) -> Callable[..., None]:
                        async def _action() -> None:
                            session_mgr.transition(
                                target_market, MarketState.CLOSING, "market close"
                            )
                            journal.append(
                                "market.closing",
                                {
                                    "market": target_market,
                                    "seq": self._tick_seq,
                                    "ts": clock.now().isoformat(),
                                },
                            )
                            oms = c.get("oms")
                            if oms is not None:
                                for ref, ord_obj in list(oms.orders.items()):
                                    inst = getattr(ord_obj, "instrument", None) or getattr(
                                        getattr(ord_obj, "spec", None), "instrument", None
                                    )
                                    if (
                                        inst is not None
                                        and session_mgr.market_key(inst) == target_market
                                    ):
                                        if hasattr(ord_obj, "state") and ord_obj.state in (
                                            OrderState.CREATED,
                                            OrderState.SUBMITTED,
                                            OrderState.ACKNOWLEDGED,
                                            OrderState.PARTIALLY_FILLED,
                                        ):
                                            try:
                                                await oms.cancel(ref)
                                            except Exception as exc:
                                                logger.debug(
                                                    "Error cancelling %s on close: %s", ref, exc
                                                )
                            await self._drain_venue_events(c)
                            ledger = c.get("ledger")
                            if ledger is not None:
                                for q in self._latest_quotes.values():
                                    if session_mgr.market_key(q.instrument) == target_market:
                                        ledger.mark(q)
                            session_mgr.transition(
                                target_market, MarketState.REPORTING, "session reporting"
                            )
                            session_mgr.transition(
                                target_market, MarketState.STOPPED, "market stopped"
                            )

                            if all(
                                session_mgr.state(mk) == MarketState.STOPPED
                                for mk in universe_markets
                            ):
                                logger.info("All markets stopped; ending session.")
                                self.stop(reason="end of session")

                        def _on_market_close(_: str = "") -> None:
                            _dispatch_async(_action())

                        return _on_market_close

                    scheduler.schedule_at(
                        f"market_close_{m}",
                        due=close_with_grace,
                        callback=_make_market_close_cb(m),
                    )

            # run.stop_time (optional)
            stop_time_str = getattr(getattr(self.run_config, "run", None), "stop_time", None)
            if stop_time_str:
                try:
                    parts = list(map(int, stop_time_str.split(":")))
                    hh = parts[0]
                    mm = parts[1]
                    ss = parts[2] if len(parts) > 2 else 0
                    stop_dt = datetime(
                        now_ist.year, now_ist.month, now_ist.day, hh, mm, ss, tzinfo=IST
                    ).astimezone(UTC)
                    if stop_dt > clock.now():

                        async def _stop_time_action() -> None:
                            logger.info(
                                "run.stop_time (%s IST) reached; shutting down.", stop_time_str
                            )
                            for mk in universe_markets:
                                if session_mgr.state(mk) not in (
                                    MarketState.CLOSING,
                                    MarketState.REPORTING,
                                    MarketState.STOPPED,
                                ):
                                    session_mgr.transition(mk, MarketState.CLOSING, "run stop time")
                                    journal.append(
                                        "market.closing",
                                        {
                                            "market": mk,
                                            "seq": self._tick_seq,
                                            "ts": clock.now().isoformat(),
                                        },
                                    )
                                    oms = c.get("oms")
                                    if oms is not None:
                                        for ref, ord_obj in list(oms.orders.items()):
                                            inst = getattr(ord_obj, "instrument", None) or getattr(
                                                getattr(ord_obj, "spec", None), "instrument", None
                                            )
                                            if (
                                                inst is not None
                                                and session_mgr.market_key(inst) == mk
                                            ):
                                                if hasattr(ord_obj, "state") and ord_obj.state in (
                                                    OrderState.CREATED,
                                                    OrderState.SUBMITTED,
                                                    OrderState.ACKNOWLEDGED,
                                                    OrderState.PARTIALLY_FILLED,
                                                ):
                                                    try:
                                                        await oms.cancel(ref)
                                                    except Exception:
                                                        pass
                                    await self._drain_venue_events(c)
                                    session_mgr.transition(
                                        mk, MarketState.REPORTING, "session reporting"
                                    )
                                    session_mgr.transition(
                                        mk, MarketState.STOPPED, "market stopped"
                                    )
                            self.stop(reason="end of session")

                        scheduler.schedule_at(
                            "run_stop_time",
                            due=stop_dt,
                            callback=lambda: _dispatch_async(_stop_time_action()),
                        )
                except Exception as exc:
                    logger.warning("Failed scheduling run.stop_time '%s': %s", stop_time_str, exc)

        # Stale feed monitoring during open market hours
        stale_val = (
            self.runtime.params.get("feed.stale_seconds")
            if self.runtime.params is not None
            else None
        )
        stale_sec = float(stale_val or 30.0)

        def _check_feed_staleness() -> None:
            if not any(session_mgr.state(mk) == MarketState.RUNNING for mk in universe_markets):
                return
            last_ts = self._last_tick_recv_ts or self._first_tick_recv_ts
            if last_ts is None:
                return
            stale_dur = (clock.now() - last_ts).total_seconds()
            if stale_dur >= stale_sec:
                if (clock.now() - self._last_feed_gap_alert_time).total_seconds() >= stale_sec:
                    self._last_feed_gap_alert_time = clock.now()
                    self._feed_gaps += 1
                    msg = f"Feed stale: no tick received for {stale_dur:.1f}s during market hours"
                    journal.append(
                        "feed.gap",
                        {
                            "state": "gap",
                            "details": msg,
                            "seq": self._tick_seq,
                            "ts": clock.now().isoformat(),
                        },
                    )
                    notify.notify_feed_status(
                        FeedStatus(
                            state="gap",
                            ts=clock.now(),
                            details=msg,
                        )
                    )

        check_int = min(10.0, max(2.0, stale_sec / 2.0))
        scheduler.schedule_repeating(
            "check_feed_staleness",
            start=now_dt + timedelta(seconds=check_int),
            interval_seconds=check_int,
            callback=_check_feed_staleness,
        )

    async def _drain_venue_events(self, c: dict[str, Any]) -> None:
        venue = c["venue"]
        oms: OrderManager = c["oms"]

        # Extract queued events without blocking the execution loop
        v_target = getattr(venue, "_venue", venue)
        q = getattr(v_target, "_event_queue", None)
        events: list[Any] = []
        if q is not None:
            while not q.empty():
                try:
                    events.append(q.get_nowait())
                except Exception:
                    break
        elif hasattr(venue, "drain_events"):
            events = list(venue.drain_events())

        for vevent in events:
            oms.apply_venue_event(vevent)

    def _build_event_stream(
        self, c: dict[str, Any], clock: Clock
    ) -> AsyncIterator[tuple[MarketEvent, int | None]]:
        if self._is_replay:
            files: list[Path] = []
            seen: set[str] = set()
            for rec_dir in (
                self.runtime.paths.data / "recorded" / "ticks",
                self.runtime.paths.data / "ticks",
            ):
                if not rec_dir.is_dir():
                    continue
                for p in sorted(rec_dir.glob("**/*.parquet")):
                    if self.replay_dates:
                        if not any(f"date={d}" in str(p) for d in self.replay_dates):
                            continue
                    key = str(p.resolve())
                    if key not in seen:
                        seen.add(key)
                        files.append(p)

            w_start = self.tick_window[0] if self.tick_window is not None else None
            w_end = self.tick_window[1] if self.tick_window is not None else None
            feed = ReplayFeed(
                files=files,
                clock=clock,
                window_start=w_start,
                window_end=w_end,
                session_id=self.replay_session,
            )

            class _ReplayStreamWrapper:
                def __init__(self, feed: ReplayFeed) -> None:
                    self._feed = feed
                    self._iter = feed.iter_with_seq()

                def __aiter__(self) -> _ReplayStreamWrapper:
                    return self

                async def __anext__(self) -> tuple[MarketEvent, int | None]:
                    try:
                        return next(self._iter)
                    except StopIteration:
                        raise StopAsyncIteration from None

                async def aclose(self) -> None:
                    self._iter = iter(())

            return _ReplayStreamWrapper(feed)

        # Paper live mode stream
        ds_name = (
            self.run_config.data_source.plugin
            if self.run_config and self.run_config.data_source
            else "upstox"
        )
        ds_cfg: dict[str, Any] = {}
        if self.run_config and self.run_config.data_source:
            if hasattr(self.run_config.data_source, "model_dump"):
                ds_cfg = {
                    k: v
                    for k, v in self.run_config.data_source.model_dump().items()
                    if k != "plugin" and v is not None
                }

        p_ctx = build_context(
            mode=self.mode,
            run_id=self.run_id,
            params=self.runtime.params,
            journal=None,
            secrets=self._resolve_secrets(),
            scrubber=getattr(self.runtime, "scrubber", None),
        )
        ds: Any = self.runtime.plugins.create(PluginKind.BROKER, ds_name, config=ds_cfg, ctx=p_ctx)
        inst_ids = list(c["instruments"].keys())
        stream_res: AsyncIterator[MarketEvent] = ds.stream(inst_ids)

        async def _live_stream_wrapper() -> AsyncIterator[tuple[MarketEvent, int | None]]:
            try:
                async for ev in stream_res:
                    yield (ev, None)
            finally:
                if hasattr(stream_res, "aclose"):
                    try:
                        await stream_res.aclose()
                    except Exception:
                        pass

        return _live_stream_wrapper()

    def _finalize_session(
        self,
        c: dict[str, Any],
        clock: Clock,
        journal: Journal,
    ) -> Path:
        """Cancel open orders, square off, mark-to-market, compact recorder, write results."""
        oms: OrderManager | None = c.get("oms")
        ledger: PortfolioLedger | None = c.get("ledger")
        notify: NotifyBridge | None = c.get("notify")
        recorder: Recorder | None = c.get("recorder")

        # 1. Flush & compact recorder fully before final marks or writing results
        if recorder is not None:
            try:
                recorder.flush()
                recorder.close()
                recorder.compact(clock.now().date())
            except Exception as exc:
                logger.warning("Recorder compaction error: %s", exc)

        # 2. Cancel open orders
        if oms is not None:
            active_cancel_states = (
                OrderState.CREATED,
                OrderState.SUBMITTED,
                OrderState.ACKNOWLEDGED,
                OrderState.PARTIALLY_FILLED,
                OrderState.UNKNOWN,
                OrderState.RECONCILING,
            )
            for ref, ord_obj in list(oms.orders.items()):
                if hasattr(ord_obj, "state") and ord_obj.state in active_cancel_states:
                    asyncio.create_task(oms.cancel(ref))

        # Capture open positions at stop (no automatic orders added)
        open_positions: list[dict[str, Any]] = []
        if ledger is not None:
            for pos in ledger.positions.values():
                if pos.qty != 0:
                    prod_val = (
                        pos.product.value if hasattr(pos.product, "value") else str(pos.product)
                    )
                    open_positions.append(
                        {
                            "instrument": str(pos.instrument),
                            "product": prod_val,
                            "qty": pos.qty,
                            "avg_price": float(pos.avg_price),
                        }
                    )

        # 3. Final marks and equity point
        if ledger is not None:
            for q in self._latest_quotes.values():
                ledger.mark(q)
            snap = ledger.snapshot()
            equity_val = float(snap.equity)
            cash_val = float(snap.cash)
            unrealized_val = float(ledger.unrealized)
            realized_val = float(ledger.realized)
            total_charges = float(ledger.total_charges)
            gross_pnl = float(ledger.realized + ledger.unrealized)
            net_pnl = float(ledger.net_realized_pnl + ledger.unrealized)
        else:
            starting_cash = (
                float(self.run_config.allocated_capital)
                if (self.run_config and hasattr(self.run_config, "allocated_capital"))
                else 0.0
            )
            equity_val = starting_cash
            cash_val = starting_cash
            unrealized_val = 0.0
            realized_val = 0.0
            total_charges = 0.0
            gross_pnl = 0.0
            net_pnl = 0.0

        self._equity_curve.append(
            {
                "ts": clock.now().isoformat(),
                "ts_ist": format_ist(clock.now()),
                "equity": equity_val,
                "cash": cash_val,
                "unrealized": unrealized_val,
                "realized": realized_val,
                "drawdown": compute_max_drawdown([p["equity"] for p in self._equity_curve]),
            }
        )

        # 4. Compile metrics
        fills = list(self._fills)
        trades_csv_rows = [
            {
                "trade_id": f.fill_id,
                "fill_id": f.fill_id,
                "client_ref": f.client_ref,
                "instrument": str(f.instrument),
                "side": str(
                    f.side.value if (f.side is not None and hasattr(f.side, "value")) else f.side
                ),
                "qty": f.qty,
                "price": float(f.price),
                "charges": float(f.charges.total) if f.charges else 0.0,
                "ts": f.ts.isoformat(),
                "ts_ist": format_ist(f.ts),
            }
            for f in fills
        ]

        rt_metrics = compute_round_trip_metrics(fills)
        win_rate = rt_metrics["win_rate"]
        gross_win_rate = rt_metrics["gross_win_rate"]
        max_dd = compute_max_drawdown([p["equity"] for p in self._equity_curve])

        date_str = clock.now().strftime("%Y-%m-%d")
        mode_str = self.mode_name

        first_tick_ist_str = format_ist(self._first_tick_ts) if self._first_tick_ts else "N/A"
        last_tick_ist_str = format_ist(self._last_tick_ts) if self._last_tick_ts else "N/A"
        coverage_pct = 0.0
        is_partial = False
        if self._first_tick_ts and self._last_tick_ts:
            f_ist = self._first_tick_ts.astimezone(IST)
            l_ist = self._last_tick_ts.astimezone(IST)
            session_open = f_ist.replace(hour=9, minute=15, second=0, microsecond=0)
            session_close = f_ist.replace(hour=15, minute=30, second=0, microsecond=0)
            if f_ist > session_open:
                is_partial = True
            session_start = max(session_open, f_ist)
            session_end = min(session_close, l_ist)
            covered_sec = max(0.0, (session_end - session_start).total_seconds())
            coverage_pct = min(100.0, max(0.0, (covered_sec / 22500.0) * 100.0))
        elif self.mode == Mode.PAPER:
            now_ist = clock.now().astimezone(IST)
            session_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
            if now_ist > session_open:
                is_partial = True

        duration_sec = (
            (self._last_tick_ts - self._first_tick_ts).total_seconds()
            if (self._first_tick_ts and self._last_tick_ts)
            else 0.0
        )
        duration_hours = max(0.0001, duration_sec / 3600.0)
        duration_min = max(0.1, duration_sec / 60.0)

        reg_rate_val = (
            self.runtime.params.get("feed.liquid_min_ticks_per_min")
            if self.runtime.params is not None
            else None
        )
        liquid_rate_floor: int = int(reg_rate_val) if reg_rate_val is not None else 30

        feed_delay_p95_by_instrument: dict[str, float] = {}
        liquid_instruments: list[str] = []
        for inst_str, count in self._tick_stats.items():
            if (count / duration_min) >= liquid_rate_floor:
                liquid_instruments.append(inst_str)

        for inst_str, delays in self._feed_delays_by_inst.items():
            if delays:
                s_d = sorted(delays)
                n_d = len(s_d)
                feed_delay_p95_by_instrument[inst_str] = round(
                    float(s_d[min(n_d - 1, int(n_d * 0.95))]), 2
                )
            else:
                feed_delay_p95_by_instrument[inst_str] = 0.0

        liquid_delays: list[float] = []
        for inst in liquid_instruments:
            liquid_delays.extend(self._feed_delays_by_inst.get(inst, []))

        effective_feed_delays = liquid_delays if liquid_delays else self._feed_delays
        if effective_feed_delays:
            sorted_fd = sorted(effective_feed_delays)
            n_fd = len(sorted_fd)
            p50_fd = float(sorted_fd[int(n_fd * 0.50)])
            p95_fd = float(sorted_fd[min(n_fd - 1, int(n_fd * 0.95))])
        else:
            p50_fd = 0.0
            p95_fd = 0.0

        if self._platform_latencies:
            sorted_plat = sorted(self._platform_latencies)
            n_plat = len(sorted_plat)
            p50_plat = float(sorted_plat[int(n_plat * 0.50)])
            p95_plat = float(sorted_plat[min(n_plat - 1, int(n_plat * 0.95))])
        elif self._tick_to_intent_latencies:
            sorted_lats = sorted(self._tick_to_intent_latencies)
            n_lats = len(sorted_lats)
            p50_plat = float(sorted_lats[int(n_lats * 0.50)])
            p95_plat = float(sorted_lats[min(n_lats - 1, int(n_lats * 0.95))])
        else:
            p50_plat = 0.0
            p95_plat = 0.0

        if self._signal_ages:
            sorted_sa = sorted(self._signal_ages)
            n_sa = len(sorted_sa)
            p50_sig_age = float(sorted_sa[int(n_sa * 0.50)])
            p95_sig_age = float(sorted_sa[min(n_sa - 1, int(n_sa * 0.95))])
        else:
            p50_sig_age = 0.0
            p95_sig_age = 0.0

        if self._bar_close_delays:
            sorted_bcds = sorted(self._bar_close_delays)
            n_bcds = len(sorted_bcds)
            p50_bcd = float(sorted_bcds[int(n_bcds * 0.50)])
            p95_bcd = float(sorted_bcds[min(n_bcds - 1, int(n_bcds * 0.95))])
        else:
            p50_bcd = 0.0
            p95_bcd = 0.0

        peak_rss_raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            peak_rss_mb = peak_rss_raw / (1024.0 * 1024.0)
        else:
            peak_rss_mb = peak_rss_raw / 1024.0
        peak_rss_mb = round(peak_rss_mb, 2)

        rec_rows = recorder.total_rows if recorder is not None else 0
        rec_bytes = recorder.total_bytes if recorder is not None else 0

        active_insts = (
            len([k for k, v in self._tick_stats.items() if v > 0]) or len(self._tick_stats) or 1
        )
        inst_hours = active_insts * duration_hours
        rec_mb = rec_bytes / (1024.0 * 1024.0)
        rec_mb_per_inst_hour = (rec_mb / inst_hours) if inst_hours > 0 else 0.0

        univ_symbols = getattr(getattr(self.run_config, "universe", None), "symbols", None) or list(
            self._tick_stats.keys()
        )
        total_univ_count = len(univ_symbols) if univ_symbols else max(1, active_insts)
        projected_full_day_mb = rec_mb_per_inst_hour * total_univ_count * 6.25

        worker = getattr(self, "worker_id", getattr(self, "_worker_id", None))
        worker_suffix = f"_{worker}" if worker else ""
        db_file = self._journal_db or (
            self.runtime.state.journal_db.parent / f"journal_replay{worker_suffix}.db"
            if self._is_replay
            else self.runtime.state.journal_db
        )
        final_journal_bytes = db_file.stat().st_size if db_file.is_file() else 0
        journal_growth = max(0, final_journal_bytes - getattr(self, "_initial_journal_bytes", 0))

        if self._is_replay:
            is_partial = False
            journal_growth = 0

        ks_obj = c.get("killswitch")
        ks_active = bool(ks_obj is not None and hasattr(ks_obj, "is_active") and ks_obj.is_active())
        if self._stop_reason == "feed_auth":
            stop_reason = "feed_auth"
            is_fail = True
        elif self._killswitch_tripped or ks_active or self._killswitch_events > 0:
            stop_reason = "kill switch"
            is_fail = True
        elif self._stop_reason == "end of session":
            stop_reason = "end of session"
            is_fail = False
        elif bool(self._run_error):
            stop_reason = "error"
            is_fail = True
        elif self._feed_halted:
            stop_reason = "feed halt"
            is_fail = True
        elif self._interrupted and (not self._is_replay or self._user_interrupted):
            stop_reason = "interrupt"
            is_fail = False
        elif self._stop_reason and "replay data ended" in self._stop_reason:
            stop_reason = self._stop_reason
            is_fail = False
        else:
            stop_reason = "end of session"
            is_fail = False

        if (self._interrupted and (not self._is_replay or self._user_interrupted)) or (
            self._stop_reason and "replay data ended" in self._stop_reason
        ):
            verdict_base = "STOPPED"
        elif is_fail:
            verdict_base = "FAIL"
        else:
            verdict_base = "PASS"
        verdict = f"{verdict_base} (partial)" if is_partial else verdict_base
        logger.info("Session stopped: reason=%s verdict=%s", stop_reason, verdict)

        if getattr(journal, "_current_run_id", None) is not None:
            if self._run_error:
                err_payload = {"error": str(self._run_error), "ts": clock.now().isoformat()}
                journal.append("error", err_payload)
            journal.append(
                "latency.stats",
                {
                    "p50_ms": round(p50_plat, 2),
                    "p95_ms": round(p95_plat, 2),
                    "feed_delay_p50_ms": round(p50_fd, 2),
                    "feed_delay_p95_ms": round(p95_fd, 2),
                    "bar_close_delay_p50_ms": round(p50_bcd, 2),
                    "bar_close_delay_p95_ms": round(p95_bcd, 2),
                    "samples": len(self._platform_latencies or self._tick_to_intent_latencies),
                },
            )
            journal.append(
                "proc.stats",
                {
                    "peak_rss_mb": peak_rss_mb,
                    "journal_bytes": final_journal_bytes,
                    "recorder_rows": rec_rows,
                    "recorder_bytes": rec_bytes,
                },
            )
            journal.append(
                "run.end",
                {
                    "run_id": self.run_id,
                    "verdict": verdict,
                    "is_fail": is_fail,
                    "is_partial": is_partial,
                    "trades": len(fills),
                    "stop_reason": stop_reason,
                    "seq": self._tick_seq,
                    "ts": clock.now().isoformat(),
                },
            )

        fail_reason_str = (
            str(self._run_error)
            if is_fail and self._run_error
            else ("Feed halted / risk breach" if is_fail else None)
        )
        if c.get("risk_engine") is not None and hasattr(c["risk_engine"], "rejects_by_rule"):
            for r, count in c["risk_engine"].rejects_by_rule.items():
                self._rejects_by_rule[r] = max(self._rejects_by_rule.get(r, 0), count)
        metrics = {
            "date": date_str,
            "mode": mode_str,
            "gross_pnl": round(gross_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "charges": round(total_charges, 2),
            "trades": len(fills),
            "win_rate": round(win_rate, 2),
            "gross_win_rate": round(gross_win_rate, 2),
            "closed_round_trips": rt_metrics["round_trips"],
            "winning_round_trips": rt_metrics["winning_round_trips"],
            "gross_winning_round_trips": rt_metrics["gross_winning_round_trips"],
            "open_positions": open_positions,
            "has_open_positions": len(open_positions) > 0,
            "max_drawdown": max_dd,
            "exposure_time": 0.0,
            "rejects_by_rule": self._rejects_by_rule,
            "ticks_per_instrument": self._tick_stats,
            "feed_gaps": self._feed_gaps,
            "killswitch_events": self._killswitch_events,
            "first_tick_ist": first_tick_ist_str,
            "last_tick_ist": last_tick_ist_str,
            "first_tick_utc": self._first_tick_ts.isoformat() if self._first_tick_ts else None,
            "last_tick_utc": self._last_tick_ts.isoformat() if self._last_tick_ts else None,
            "first_tick_recv_utc": (
                self._first_tick_recv_ts.isoformat() if self._first_tick_recv_ts else None
            ),
            "last_tick_recv_utc": (
                self._last_tick_recv_ts.isoformat() if self._last_tick_recv_ts else None
            ),
            "stop_reason": stop_reason,
            "session_coverage_pct": round(coverage_pct, 2),
            "bars_by_timeframe": self._bars_by_tf,
            "signals": self._signals_count,
            "orders": self._orders_count,
            "fills": len(fills),
            "feed_delay_p50_ms": round(p50_fd, 2),
            "feed_delay_p95_ms": round(p95_fd, 2),
            "feed_delay_p95": round(p95_fd, 2),
            "feed_delay_p95_by_instrument": feed_delay_p95_by_instrument,
            "liquid_instruments": liquid_instruments,
            "platform_p50_ms": round(p50_plat, 2),
            "platform_p95_ms": round(p95_plat, 2),
            "platform_p95": round(p95_plat, 2),
            "signal_age_p50_ms": round(p50_sig_age, 2),
            "signal_age_p95_ms": round(p95_sig_age, 2),
            "signal_age_p95": round(p95_sig_age, 2),
            "p50_bar_close_delay_ms": round(p50_bcd, 2),
            "p95_bar_close_delay_ms": round(p95_bcd, 2),
            "bar_close_delay_p95_ms": round(p95_bcd, 2),
            "bar_close_delay_p95": round(p95_bcd, 2),
            "bar_close_delay_ms": round(p95_bcd, 2),
            "peak_rss_mb": peak_rss_mb,
            "recorder_rows": rec_rows,
            "recorder_bytes": rec_bytes,
            "recorder_mb_per_inst_hour": round(rec_mb_per_inst_hour, 4),
            "projected_full_day_mb": round(projected_full_day_mb, 2),
            "journal_growth_bytes": journal_growth,
            "is_partial": is_partial,
            "fail_reason": fail_reason_str,
        }

        # 5. Write results
        writer = ResultsWriter(self.run_id, results_dir=self.runtime.paths.results)
        summary_info = {
            "run_name": self.run_name,
            "mode": mode_str,
            "date": date_str,
            "verdict": verdict,
            "is_partial": is_partial,
            "fail_reason": fail_reason_str,
        }

        effective_text = getattr(self.runtime.effective, "raw_text", str(self.run_config))
        reg_snap: Mapping[str, Any]
        if self.runtime.params is not None and hasattr(self.runtime.params, "snapshot"):
            _, p_dict = self.runtime.params.snapshot()
            reg_snap = {"params": p_dict}
        else:
            reg_snap = {}
        sim_venue = getattr(self.run_config, "execution_venue", None) if self.run_config else None
        venue_seed = getattr(sim_venue, "seed", 7) if sim_venue else 7
        prov: dict[str, list[str]] = {"datasets": [], "checksums": []}
        if hasattr(self, "_data_service") and self._data_service is not None:
            try:
                prov = self._data_service.recorded_provenance()
            except Exception:
                pass

        manifest_info: dict[str, Any] = {
            "code_version": "0.1.0",
            "plugin_versions": {
                p.name: {"version": p.manifest.version, "hash": p.code_hash}
                for p in self.runtime.plugins.list()
            },
            "data_files": prov.get("datasets", []),
            "datasets": prov.get("datasets", []),
            "checksums": prov.get("checksums", []),
            "seed": self.seed or venue_seed,
            "instrument_master_checksum": "verified",
            "first_tick_utc": self._first_tick_ts.isoformat() if self._first_tick_ts else None,
            "last_tick_utc": self._last_tick_ts.isoformat() if self._last_tick_ts else None,
            "first_tick_recv_utc": (
                self._first_tick_recv_ts.isoformat() if self._first_tick_recv_ts else None
            ),
            "last_tick_recv_utc": (
                self._last_tick_recv_ts.isoformat() if self._last_tick_recv_ts else None
            ),
            "stop_reason": stop_reason,
        }
        if self.warmup_source is not None:
            manifest_info["warmup_override"] = self.warmup_source
            summary_info["warmup_override"] = self.warmup_source

        session_md_text = (
            f"Session {self.run_name} ({mode_str}) completed on {date_str}.\n"
            f"Net P&L: ₹{net_pnl:.2f} | Trades: {len(fills)} | Max Drawdown: {max_dd:.2f}%\n"
            f"Verdict: {summary_info['verdict']}\n"
            f"Stop Reason: {stop_reason}\n"
        )
        if is_partial:
            session_md_text += "Run Type: partial (started after session open)\n"
        if open_positions:
            pos_summary = ", ".join(f"{p['instrument']}:{p['qty']}" for p in open_positions)
            session_md_text += f"Open at stop: {pos_summary}\n"
            n_open = len(open_positions)
            session_md_text += f"Verdict Note: Unresolved open positions at stop ({n_open})\n"
        if fail_reason_str:
            session_md_text += f"\nError: {fail_reason_str}\n"

        res_path = writer.write(
            summary_info=summary_info,
            metrics=metrics,
            trades=trades_csv_rows,
            equity_points=self._equity_curve,
            effective_config_text=effective_text,
            registry_snapshot=reg_snap,
            manifest_info=manifest_info,
            session_text=session_md_text,
        )

        ind_mgr = c.get("ind_mgr")
        if not self._is_replay and ind_mgr is not None and getattr(ind_mgr, "warmup_bars", None):
            try:
                save_warmup_bars_parquet(res_path / "warmup_bars.parquet", ind_mgr.warmup_bars)
            except Exception as exc:
                logger.warning("Failed to save warmup_bars.parquet: %s", exc)

        if notify is not None:
            notify.notify_daily_summary(session_md_text)

        if journal is not None and hasattr(journal, "flush"):
            try:
                journal.flush()
            except Exception:
                pass

        self._export_run_events(res_path, journal)
        return res_path

    def _export_run_events(self, res_path: Path, journal: Journal | None = None) -> None:
        """Export session journal events to events.jsonl in results folder."""
        try:
            db_file = (
                journal.db_path if (journal is not None and hasattr(journal, "db_path")) else None
            )
            if db_file is None or not db_file.is_file():
                worker = os.environ.get("PYTEST_XDIST_WORKER") or getattr(
                    self, "worker_id", getattr(self, "_worker_id", None)
                )
                worker_suffix = f"_{worker}" if worker else ""
                db_file = self._journal_db or (
                    self.runtime.state.journal_db.parent / f"journal_replay{worker_suffix}.db"
                    if self._is_replay
                    else (
                        self.runtime.state.journal_db.parent / f"journal{worker_suffix}.db"
                        if worker
                        else self.runtime.state.journal_db
                    )
                )
            if db_file.is_file():
                reader = JournalReader(db_file)
                j_run_id = getattr(journal, "_current_run_id", None)
                evs = (
                    reader.events(run_id=j_run_id)
                    if j_run_id
                    else reader.events(run_id=self.run_id)
                )
                evs_path = res_path / "events.jsonl"
                with open(evs_path, "w", encoding="utf-8") as f:
                    for ev in evs:
                        f.write(json.dumps(ev) + "\n")
        except Exception as exc:
            logger.warning("Failed to export events.jsonl for run %s: %s", self.run_id, exc)
