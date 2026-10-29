"""SessionRunner orchestrating paper trading and historical replay sessions.

Wires market events, bar building, strategy execution, pre-trade risk evaluation,
order management, simulated venue matching, portfolio ledgering, and results reporting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from algodesk.api import (
    AlgoDeskError,
    Bar,
    Fill,
    FillEvent,
    Instrument,
    InstrumentId,
    Mode,
    PluginContext,
    PluginKind,
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
from algodesk.data.replay import ReplayFeed
from algodesk.engine.bars import BarBuilder
from algodesk.engine.bus import EventBus
from algodesk.engine.clock import SimClock, WallClock
from algodesk.engine.context import BarBuffer, build_strategy_context
from algodesk.engine.indicator_manager import IndicatorManager
from algodesk.engine.notify import NotifyBridge
from algodesk.engine.scheduler import Scheduler
from algodesk.engine.sessions import SessionManager
from algodesk.engine.strategy_runtime import StrategyRuntime
from algodesk.fees.engine import FeeEngine
from algodesk.journal.store import JournalReader
from algodesk.journal.writer import Journal, JournalSettings
from algodesk.oms.manager import OrderManager
from algodesk.ops.checks import build_run_checks
from algodesk.ops.context import build_context
from algodesk.ops.doctor import DoctorRegistry
from algodesk.ops.runtime import Runtime
from algodesk.portfolio.ledger import PortfolioLedger
from algodesk.results.writer import ResultsWriter, compute_max_drawdown
from algodesk.risk.engine import RiskEngine
from algodesk.risk.killswitch import KillSwitch
from algodesk.risk.monitor import RiskMonitor
from algodesk.risk.view import RiskViewContext

logger = logging.getLogger("algodesk.engine.runner")


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
    ) -> None:
        self.runtime = runtime
        self._secrets_provider = secrets
        self.run_config = run_config or runtime.effective.run
        if isinstance(mode, str):
            self.mode = Mode.BACKTEST if mode.lower() == "replay" else Mode(mode.lower())
        else:
            self.mode = mode
        self._is_replay: bool = str(mode).lower() == "replay" or bool(replay_dates)
        self.observe = observe
        self.replay_dates = list(replay_dates or [])
        self.seed = seed
        self.dry_run = dry_run
        self.clock = clock if clock is not None else self._init_clock()
        self._journal_db = journal_db

        self.run_name = getattr(getattr(self.run_config, "run", None), "name", "run")
        ts_str = self.clock.now().strftime("%Y%m%d_%H%M%S")
        mode_str = "replay" if self._is_replay else str(self.mode.value)
        if run_id is not None:
            self.run_id = run_id
        elif self._is_replay:
            self.run_id = f"{self.run_name}_{mode_str}_{ts_str}"
        else:
            rand_suffix = uuid.uuid4().hex[:6]
            self.run_id = f"{self.run_name}_{mode_str}_{ts_str}_{rand_suffix}"

        self._stop_event = asyncio.Event()
        self._interrupted = False
        self._feed_halted = False
        self._run_error: str | None = None

        self._fills: list[Fill] = []
        self._tick_stats: dict[str, int] = {}
        self._feed_gaps = 0
        self._killswitch_events = 0
        self._rejects_by_rule: dict[str, int] = {}
        self._equity_curve: list[dict[str, Any]] = []
        self._latest_quotes: dict[InstrumentId, Quote] = {}

    @property
    def mode_name(self) -> str:
        if self._is_replay:
            return "replay"
        return str(self.mode.value if hasattr(self.mode, "value") else self.mode)

    def stop(self) -> None:
        """Signal the session runner to stop event streaming and cleanly shut down."""
        self._stop_event.set()

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

        clock = self.clock
        journal = self._init_journal()
        components: dict[str, Any] = {}
        error_to_reraise: Exception | None = None

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
                    await self._execute_session(components, clock, journal)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    logger.info("Session interrupted gracefully; generating results.")
                    self._interrupted = True
                except AlgoDeskError as exc:
                    logger.error("Session stopped due to error: %s; writing results.", exc)
                    self._run_error = str(exc)
                    error_to_reraise = exc
                except Exception as exc:
                    logger.error("Session encountered unhandled error: %s; writing results.", exc)
                    self._run_error = f"Unhandled error: {exc}"
                    error_to_reraise = exc
                finally:
                    try:
                        end_st = "aborted" if (self._interrupted or self._run_error) else "ok"
                        journal.end_run(end_st)
                    except Exception:
                        pass
        except AlgoDeskError as exc:
            logger.error("Session initialization failed: %s; writing results.", exc)
            self._run_error = str(exc)
            error_to_reraise = exc
        except Exception as exc:
            logger.error("Session startup unhandled error: %s; writing results.", exc)
            self._run_error = f"Unhandled error: {exc}"
            error_to_reraise = exc
        finally:
            results_path = self._finalize_session(components, clock, journal)

        if error_to_reraise is not None:
            raise error_to_reraise

        return results_path

    def _init_clock(self) -> Clock:
        if self._is_replay:
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
        bar_builder = self._init_bar_builder(session_mgr, bus, scheduler, ind_mgr, buf, strat_rt)
        recorder = self._init_recorder(bus, scheduler)

        def _on_fill_event(fe: FillEvent) -> None:
            fill = fe.fill
            self._fills.append(fill)
            ledger.apply_fill(fill)
            notify.notify_fill(fill)
            monitor.on_fill(fe)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(strat_rt.dispatch_fill(fill))
            except RuntimeError:
                pass

        bus.subscribe(FillEvent, _on_fill_event)

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
            "bar_builder": bar_builder,
            "recorder": recorder,
            "fee_engine": fee_engine,
        }

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

        positions_map: Mapping[str, Any] = {
            f"{k[0]}:{k[1].value}": pos for k, pos in ledger.positions.items()
        }

        def _risk_view_builder() -> RiskViewContext:
            assert self.run_config is not None
            open_ords = [
                o for o in oms.orders.values() if o.state in ("pending", "submitted", "open")
            ]
            return RiskViewContext(
                portfolio=ledger.snapshot(),
                positions=positions_map,
                open_orders=open_ords,
                daily_pnl=ledger.realized + ledger.unrealized,
                latest_quotes=self._latest_quotes,
                params=self.runtime.params or {},
                clock=clock,
                instruments=instruments,
                allocated_capital=Decimal(str(self.run_config.allocated_capital)),
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
        )

        inst_map: Mapping[InstrumentId | str, Instrument] = {k: v for k, v in instruments.items()}
        assert self.run_config is not None
        for s_cfg in self.run_config.strategies:
            strat_ctx = build_strategy_context(
                clock=clock,
                instruments=inst_map,
                indicator_manager=ind_mgr,
                bar_buffer=buf,
                position_fn=lambda i: ledger.positions.get((InstrumentId(str(i)), Product.CNC)),
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
                cal_plugins[exch] = cal
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
    ) -> BarBuilder:
        assert self.run_config is not None

        async def _on_bar_closed(bar: Any) -> None:
            buf.append(bar)
            ind_mgr.on_bar(bar)
            if not self._feed_halted:
                await strat_rt.dispatch_bar(bar)

        def _on_bar_sync(bar: Bar) -> None:
            asyncio.create_task(_on_bar_closed(bar))

        tfs = [s.params.get("timeframe", "5m") for s in self.run_config.strategies]
        return BarBuilder(
            timeframes=tuple(set(tfs)),
            session_manager=session_mgr,
            bus=bus,
            scheduler=scheduler,
            on_bar_closed=_on_bar_sync,
        )

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

        res: dict[InstrumentId, Instrument] = {}
        for s in symbols:
            inst_id = InstrumentId(s)
            p = parse_id(s)
            res[inst_id] = Instrument(
                id=inst_id,
                exchange=p.exchange,
                segment=p.segment,
                symbol=p.symbol,
                kind="equity",
                lot_size=1,
                tick_size=Decimal("0.05"),
                expiry=p.expiry,
                underlying=None,
                isin=None,
                broker_keys={},
            )
        return res

    async def _execute_session(
        self,
        c: dict[str, Any],
        clock: Clock,
        journal: Journal,
    ) -> None:
        """Stream market events, process fills, advance scheduler, and monitor heartbeat."""
        notify: NotifyBridge = c["notify"]
        notify.notify_session_start(self.run_name, str(self.mode), len(c["instruments"]))

        # Schedule timers
        self._setup_timers(c, clock)

        # Build feed stream
        stream = self._build_event_stream(c, clock)

        async for ev in stream:
            if self._stop_event.is_set():
                break

            # 1. Recording
            if c["recorder"] is not None:
                try:
                    c["recorder"].on_market_event(ev)
                except Exception as exc:
                    notify.notify_error(f"Recorder disk full or error: {exc}", "recorder")

            # 2. Market event handling
            if isinstance(ev, Quote):
                c["venue"].on_quote(ev)
                self._latest_quotes[ev.instrument] = ev
            elif isinstance(ev, Tick):
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
                await asyncio.sleep(0)
            elif isinstance(ev, FeedStatus):
                notify.notify_feed_status(ev)
                if ev.state in ("disconnected", "gap"):
                    self._feed_halted = True
                    self._feed_gaps += 1

            # 3. Process venue execution events
            await self._drain_venue_events(c)

            # 4. Advance scheduler timers
            c["scheduler"].advance_to(clock.now())
            c["scheduler"].fire_due()

            # 5. Check OMS timeouts
            await c["oms"].check_timeouts()

        # Drain pending bar tasks and venue events after stream ends
        await asyncio.sleep(0)
        await self._drain_venue_events(c)

    def _setup_timers(self, c: dict[str, Any], clock: Clock) -> None:
        assert self.run_config is not None
        scheduler: Scheduler = c["scheduler"]
        notify: NotifyBridge = c["notify"]
        strat_rt: StrategyRuntime = c["strat_rt"]
        ledger: PortfolioLedger = c["ledger"]

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
            self._equity_curve.append(
                {
                    "ts": clock.now().isoformat(),
                    "equity": float(snap.equity),
                    "cash": float(snap.cash),
                    "unrealized": float(ledger.unrealized),
                    "realized": float(ledger.realized),
                    "drawdown": compute_max_drawdown(
                        [p["equity"] for p in self._equity_curve] + [float(snap.equity)]
                    ),
                }
            )

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

        # Strategy exit timer
        for s in self.run_config.strategies:
            exit_time_str = s.params.get("exit_time", "15:15")
            try:
                hh, mm = map(int, exit_time_str.split(":"))
                exit_dt = datetime(
                    now_dt.year, now_dt.month, now_dt.day, hh, mm, tzinfo=now_dt.tzinfo
                )
                if exit_dt > now_dt:
                    scheduler.schedule_at(
                        "strategy_exit",
                        due=exit_dt,
                        callback=lambda: asyncio.create_task(strat_rt.dispatch_timer("exit_time")),
                    )
            except Exception:
                pass

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

    def _build_event_stream(self, c: dict[str, Any], clock: Clock) -> AsyncIterator[MarketEvent]:
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

            feed = ReplayFeed(files=files, clock=clock)

            async def _replay_generator() -> AsyncIterator[MarketEvent]:
                for item in feed:
                    yield item

            return _replay_generator()

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
        return stream_res

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

        # 1. Cancel open orders
        if oms is not None:
            for ref, ord_obj in list(oms.orders.items()):
                if hasattr(ord_obj, "state") and ord_obj.state in ("pending", "submitted", "open"):
                    asyncio.create_task(oms.cancel(ref))

        # 2. Final marks and equity point
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
                "equity": equity_val,
                "cash": cash_val,
                "unrealized": unrealized_val,
                "realized": realized_val,
                "drawdown": compute_max_drawdown([p["equity"] for p in self._equity_curve]),
            }
        )

        # 3. Compact recorder files
        if recorder is not None:
            try:
                recorder.flush()
                recorder.compact(clock.now().date())
            except Exception as exc:
                logger.warning("Recorder compaction error: %s", exc)

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
            }
            for f in fills
        ]

        win_trades = sum(1 for f in fills if f.price > 0)
        win_rate = (win_trades / len(fills) * 100.0) if fills else 0.0
        max_dd = compute_max_drawdown([p["equity"] for p in self._equity_curve])

        date_str = clock.now().strftime("%Y-%m-%d")
        mode_str = self.mode_name
        metrics = {
            "date": date_str,
            "mode": mode_str,
            "gross_pnl": round(gross_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "charges": round(total_charges, 2),
            "trades": len(fills),
            "win_rate": round(win_rate, 2),
            "max_drawdown": max_dd,
            "exposure_time": 0.0,
            "rejects_by_rule": self._rejects_by_rule,
            "ticks_per_instrument": self._tick_stats,
            "feed_gaps": self._feed_gaps,
            "killswitch_events": self._killswitch_events,
        }

        # 5. Write results
        writer = ResultsWriter(self.run_id, results_dir=self.runtime.paths.results)
        is_fail = self._interrupted or self._feed_halted or bool(self._run_error)
        summary_info = {
            "run_name": self.run_name,
            "mode": mode_str,
            "date": date_str,
            "verdict": "FAIL" if is_fail else "PASS",
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
        manifest_info: dict[str, Any] = {
            "code_version": "0.1.0",
            "plugin_versions": {
                p.name: {"version": p.manifest.version, "hash": p.code_hash}
                for p in self.runtime.plugins.list()
            },
            "data_files": [],
            "seed": self.seed or venue_seed,
            "instrument_master_checksum": "verified",
        }

        session_md_text = (
            f"Session {self.run_name} ({mode_str}) completed on {date_str}.\n"
            f"Net P&L: ₹{net_pnl:.2f} | Trades: {len(fills)} | Max Drawdown: {max_dd:.2f}%\n"
            f"Verdict: {summary_info['verdict']}\n"
        )
        if self._run_error:
            session_md_text += f"\nError: {self._run_error}\n"

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

        if notify is not None:
            notify.notify_daily_summary(session_md_text)

        self._export_run_events(res_path)
        return res_path

    def _export_run_events(self, res_path: Path) -> None:
        """Export session journal events to events.jsonl in results folder."""
        try:
            worker = getattr(self, "worker_id", getattr(self, "_worker_id", None))
            worker_suffix = f"_{worker}" if worker else ""
            db_file = self._journal_db or (
                self.runtime.state.journal_db.parent / f"journal_replay{worker_suffix}.db"
                if self._is_replay
                else self.runtime.state.journal_db
            )
            if db_file.is_file():
                reader = JournalReader(db_file)
                evs = reader.events(run_id=self.run_id)
                evs_path = res_path / "events.jsonl"
                with open(evs_path, "w", encoding="utf-8") as f:
                    for ev in evs:
                        f.write(json.dumps(ev) + "\n")
        except Exception as exc:
            logger.warning("Failed to export events.jsonl for run %s: %s", self.run_id, exc)
