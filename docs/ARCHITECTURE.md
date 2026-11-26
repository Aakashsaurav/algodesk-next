# Architecture

Owner: Aakash · Version: 1.1 · Date: 26 November 2026

## Summary

AlgoDesk Next is a small core plus plugins. The core is organized in strict layers: `api` at the bottom; foundation packages (`fees`, `config`, `params`, `registry`, `journal`, `logs`, `data`, `screens`); trading execution layers (`oms`, `portfolio`, `risk`, `results`); `ops` and `devtools`; runtime `engine`; and `cli` on top. Plugins import only `algodesk.api`; the core never imports a plugin. Every adjustable number lives in the parameter registry, every event in a hash-chained journal, and every plugin load passes mode, capability and conformance gates. Orders follow a strict state machine with write-ahead journaling. Risk checks evaluate without short-circuiting, backed by an atomic persistent kill-switch. Import rules are enforced by tests. Market data, strategies, orders, notifiers, and brokers arrive as plugins.

## 1. Layers

```
cli
engine
ops | devtools
oms | portfolio | risk | results
fees | config | params | registry | journal | logs | data | screens   (independent of each other)
api                                                                     (imports no other algodesk package)
plugins/  ->  algodesk.api only
```
Enforced by `import-linter` contracts in `.importlinter` (run by `scripts/check.sh`) and by `algodesk lint`.

## 2. Packages

| Package | Purpose | Main public names |
|---|---|---|
| `api` | Contracts, domain types and protocols | `Instrument`, `Quote`, `Bar`, `Order`, `Fill`, `Position`, `Portfolio`, `Indicator`, `Calendar`, `Clock`, `PluginBase`, error classes |
| `fees` | Deterministic fee calculation and schedules | `FeeEngine`, `Schedule`, `charges_for_fills`, `resolve_fee_segment_and_product` |
| `config` | Layered TOML configuration, validated | `PlatformConfig`, `RunConfig`, `load_platform`, `load_run`, `build_effective`, `render_effective`, `resolve_paths`, `ensure_dirs` |
| `params` | Central parameter registry (SQLite) | `ParamRegistry`, `ParamSpec`, `load_defaults`, `ChangeResult` |
| `registry` | Plugin discovery, validation, gating, loading | `PluginRegistry`, `discover`, `code_hash`, `ConformanceStore`, `scaffold_plugin`, `base_contract_problems` |
| `journal` | Append-only hash-chained event log (SQLite) | `Journal`, `JournalSettings`, `verify_journal` |
| `logs` | JSON logging with secret scrubbing | `setup_logging`, `get_logger` |
| `data` | Bar & tick store, coverage, validation, quarantine, resampler, catalog, rate limiter, gap planner, fetcher, data service, historical & tick feeds, quote synth | `BarStore`, `BarValidator`, `RateLimiter`, `GapPlanner`, `Fetcher`, `DataService`, `CoverageIndex`, `QuarantineManager`, `SessionBarStore`, `resample`, `DataManifest`, `DataCatalog`, `HistoricalFeed`, `TickBarFeed`, `BarQuoteSynth` |
| `screens` | Market screening engine: TOML/Python rules, feature extraction, cross-sectional ranking, reproducible run bundles | `ScreenRunner`, `TomlScreenModel`, `load_toml_screen`, `build_feature_view`, `compute_percentile_ranks`, `compute_result_hash` |
| `engine` | Deterministic event bus, clocks, scheduler, bars, runner, backtest runner, replay, and strategy runtime | `EventBus`, `WallClock`, `SimClock`, `Scheduler`, `BarBuilder`, `SessionManager`, `SessionRunner`, `BacktestRunner`, `StrategyRuntime`, `IndicatorManager`, `NotifyBridge` |
| `oms` | Order lifecycle, state machine, write-ahead journaling | `OrderManager` |
| `portfolio` | Realized/unrealized accounting, mark-to-market, snapshots | `PortfolioLedger` |
| `risk` | Pre-trade evaluation, persistent kill-switch, loss monitor | `RiskEngine`, `KillSwitch`, `RiskMonitor`, `RiskViewContext`, `DrawdownTracker` |
| `results` | Execution performance metrics, SQLite results index, and artifact report generator | `ResultsWriter`, `ResultsIndex`, `compute_max_drawdown` |
| `ops` | Wiring and diagnostics | `open_runtime`, `Runtime`, `DoctorRegistry`, `build_core_checks` |
| `devtools` | Automatic fences | budget, docs, archlint, secretscan, `build_tool_checks` |
| `cli` | The `algodesk` command | Typer app |

## 3. Start-up sequence (`open_runtime`)

1. Find the project root; read `config/platform.toml` and layers (platform, profile, run, environment, CLI); validate; stop with a three-line error on any problem.
2. Resolve paths inside the project folder; create directories only when asked.
3. Open the parameter registry (if DB exists or creation requested) and load defaults from `config/params/*.toml`.
4. Discover plugin manifests without importing plugin code.
5. Long-running processes open the journal and start scrubbed JSON logging.

## 4. Where things live

| Path | Content |
|---|---|
| `config/` | `platform.toml`, `profiles/`, `runs/`, `params/*.toml`, `groups/*.toml`, `screens/*.toml`, lint/scan rules |
| `data/state/` | `data/state/params.db`, `data/state/journal.db`, `data/state/killswitch.json`, `data/state/data_manifest.db`, `data/state/results_index.db` (git-ignored) |
| `data/bars/` | Historical 1m (year/month Parquet) and 1d (consolidated `daily.parquet`) bars |
| `data/session_bars/` | Ephemeral live intraday session bars (`date=YYYY-MM-DD/`) |
| `data/ticks/` | Hive-partitioned Parquet tick parts and compacted `day.parquet` files |
| `data/.quarantine/`, `data/.trash/` | Quarantined invalid chunks and soft-deleted data awaiting purge |
| `results/<run_id>/` | Execution artifacts (`SUMMARY.md`, `metrics.json`, `trades.csv`, `equity.csv`, `equity_daily.csv`, `round_trips.csv`, `orders.csv`, `manifest.json`, `session.md`) |
| `logs/algodesk.log` | Daily-rotating scrubbed JSON logs (git-ignored) |
| `.algodesk/conformance.json` | Conformance pass records keyed by plugin code hash |
| `.venv/`, `.local/` | Isolated virtual environment, Python caches, and instrument master cache |
| `_workbench/` | Scratch: task cards, handoff file, design docs (never in `docs/`) |

## 5. Safety rules already implemented

- Order-capable plugins are refused in research, backtest, and paper **before import**; live requires a current conformance record.
- Paper and live loads require a conformance record for the plugin's current code hash.
- Undeclared capabilities raise `NotSupported`; no stubs return success (`algodesk lint`).
- The journal forbids updates/deletes, chains every event by hash, and `journal verify` detects tampering.
- Secrets use `secret:<name>` references; plain secrets are rejected; logs and journal payloads pass through `Scrubber`.
- Parameter changes require a reason; tightening applies immediately; loosening while armed requires typed confirmation and waits for next session.
- Environment isolation: `doctor` and `check.sh` fail if Python, caches, or packages come from outside the project folder.

## 6. Plugin loading

`PluginRegistry.create(kind, name, config=..., ctx=...)` verifies: existence, `modes_allowed`, order-capable gate (before import), conformance record (paper/live), `ConfigSchema`, lazy import under a unique module name, instantiation with `(manifest, config, ctx)`, and `Plugin` contract compliance.

## 7. Quality gates (`bash scripts/check.sh`)

Isolation, `ruff check`, `ruff format --check`, `mypy` (strict), `pytest`, `import-linter` contracts, `budget` (size advisory, dependencies hard), `docs check`, architecture lint, secret scan. Ends with `CHECK: PASS` or `CHECK: FAIL (n)`.

## 8. Data Layer

Historical market data flows through `GapPlanner`, `RateLimiter`, `Fetcher`, `BarValidator`, `BarStore`, `QuarantineManager`, `ActionsStore`/`Reconciler`, and `DataService.get`. `DataService` supports `raw`/`adjusted` downloads, dynamic corporate-action adjustment (`none`, `splits`, `total`), on-the-fly resampling, and `DataSlice` provenance. For operational workflows and detailed design, see [Runbook](RUNBOOK.md#data-routine) and `_workbench/M2 — Data Layer Module Design.md`.

## 9. Market Sessions & Calendars

Market sessions (open, close, square-off, weekdays) live under `market.<exchange>.<segment>` in `config/params/markets.toml`. Calendar plugins supply trading days, holidays, per-session closures (NSE, BSE, MCX), and special session timings.

## 10. Secrets & Broker Ingestion

Secrets providers (`keychain`, `env`) expose name-based lookup via `SecretReader`, registering retrieved values with `Scrubber`. Broker plugins implement `DataSource` and `AuthFlow`.

## 11. Strategy Runtime, Runner, Replay & Reporting

`SessionRunner` coordinates event streaming, bar building, strategy execution, order routing, risk evaluation, simulated matching, and portfolio ledgering. `StrategyRuntime` provides a read-only `StrategyContext`, dispatches lifecycle hooks, and isolates strategy exceptions. `ResultsWriter` writes the artifact bundle in `results/<run_id>/` and upserts `results_index.db`. Historical replay uses `SimClock` and `ReplayFeed` with `compare_runs` validating bit-for-bit equivalence.

## 12. Backtest mode

`BacktestRunner` composes `SessionRunner` directly (one engine, zero duplicated trading logic):
- **Components**: `SimClock` (event-driven time), `HistoricalFeed` (offline `cache_only=True` bars) or `TickBarFeed` (tick-fidelity 1m reconstruction), `BarQuoteSynth`, `venues/sim`, `FeeEngine` (date-bounded schedules), and `ResultsWriter`.
- **Quote path rule**: `BarQuoteSynth` emits 4 synthetic quotes per bar: `O -> L -> H -> C` for up bars ($C \ge O$) and `O -> H -> L -> C` for down bars ($C < O$). Daily bars align to NSE 09:15–15:30 IST; intraday closing quotes sit at `end_ts - 1ms` so boundary timers see $\le 1\text{ms}$ quote age. Intermediate L/H quotes are skipped when an instrument has no open position or resting order.
- **Open-quote fill rule**: Orders decided on bar $t$ arrive at the Open quote timestamp of bar $t+1$ (`backtest.delay_bars >= 1`, `backtest.sim_latency_ms = 0`). Market orders fill at `round_to_tick(open(t+1) +/- slippage)` independent of bar direction; resting stops crossed at open fill at open.
- **Kill-switch isolation**: Backtests use an in-memory / run-local `results/<run_id>/killswitch.json` and never read or mutate `data/state/killswitch.json`. Default `backtest.on_kill_switch = "continue"` blocks new entries on breach while allowing reducing exits.
- **Journal batch mode**: Events write to `journal_bt_<run_id>.db` filtered by `backtest.journal_events` and flushed in batches of `backtest.journal_batch_size`, preserving the hash chain.
- **Result hash**: `manifest.json` records an 8-component canonical SHA-256 `result_hash` (universe, datasets, corporate actions, fees, strategy, params, model settings/seed, code version) for bitwise reproducibility.

## 13. Structured Logging & Observability

`run`, `replay`, and `feed probe` initialize daily-rotating scrubbed JSON logs (`logs/algodesk.log`), record lifecycle/latency/resource events in SQLite, emit periodic console status lines, and generate `algodesk session report` summaries. In paper/live on macOS, `ops.keep_awake` spawns `caffeinate -i -w <pid>` and `SuspendWatcher` detects sleep jumps.

## 14. Current state & planned milestones

Milestones **M0–M4 are complete** (foundations, walking skeleton, data layer, indicators/screens, backtester/reports). Remaining milestones:
- **M5 Risk and control**: Risk profiles in registry, monitors, health guards, Telegram commands, outage policies.
- **M6 Zerodha plugin and shadow**: Zerodha venue (disabled by default), full order types, reconciliation, shadow mode.
- **M7 Remote runner**: Profile B on static-IP host, login ceremony, supervisor, backups.
- **M8 Extended paper** & **M9 Micro-live**: Multi-day paper campaigns and supervised live gates (§8.5).
