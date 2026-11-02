# Architecture

Owner: Aakash · Version: 1.0 · Date: 29 October 2026

## Summary

AlgoDesk Next is a small core plus plugins. The core is organized in strict layers: `api` at the bottom; foundation packages (`fees`, `config`, `params`, `registry`, `journal`, `logs`, `data`); trading execution layers (`oms`, `portfolio`, `risk`, `results`); `ops` and `devtools`; runtime `engine`; and `cli` on top. Plugins import only `algodesk.api`; the core never imports a plugin. Every adjustable number lives in the parameter registry, every event in a hash-chained journal, and every plugin load passes mode, capability and conformance gates. Orders follow a strict state machine with write-ahead journaling. Risk checks evaluate without short-circuiting, backed by an atomic persistent kill-switch. Import rules are enforced by tests. Market data, strategies, orders, notifiers, and brokers arrive as plugins.

## 1. Layers

```
cli
engine
ops | devtools
oms | portfolio | risk | results
fees | config | params | registry | journal | logs | data   (independent of each other)
api                                                           (imports no other algodesk package)
plugins/  ->  algodesk.api only
```
Enforced by `import-linter` contracts in `.importlinter` (run by `scripts/check.sh`) and by `algodesk lint`.

## 2. Packages

| Package | Purpose | Main public names |
|---|---|---|
| `api` | Contracts, domain types and protocols | `Instrument`, `Quote`, `Bar`, `Order`, `Fill`, `Position`, `Portfolio`, `Indicator`, `Calendar`, `Clock`, `PluginBase`, error classes |
| `fees` | Deterministic fee calculation and schedules | `FeeEngine`, `Schedule`, `charges_for_fills` |
| `config` | Layered TOML configuration, validated | `PlatformConfig`, `RunConfig`, `load_platform`, `load_run`, `build_effective`, `render_effective`, `resolve_paths`, `ensure_dirs` |
| `params` | Central parameter registry (SQLite) | `ParamRegistry`, `ParamSpec`, `load_defaults`, `ChangeResult` |
| `registry` | Plugin discovery, validation, gating, loading | `PluginRegistry`, `discover`, `code_hash`, `ConformanceStore`, `scaffold_plugin`, `base_contract_problems` |
| `journal` | Append-only hash-chained event log (SQLite) | `Journal`, `JournalSettings`, `verify_journal` |
| `logs` | JSON logging with secret scrubbing | `setup_logging`, `get_logger` |
| `data` | Bar & tick store, day coverage, validation, quarantine, resampler, catalog, groups, rate limiter, gap planner, parallel fetcher, data service | `BarStore`, `BarValidator`, `RateLimiter`, `GapPlanner`, `Fetcher`, `DataService`, `CoverageIndex`, `QuarantineManager`, `SessionBarStore`, `resample`, `DataManifest`, `DataCatalog` |
| `engine` | Deterministic event bus, clocks, scheduler, bars, runner, and strategy runtime | `EventBus`, `WallClock`, `SimClock`, `Scheduler`, `BarBuilder`, `SessionManager`, `SessionRunner`, `StrategyRuntime`, `IndicatorManager`, `NotifyBridge` |
| `oms` | Order lifecycle, state machine, write-ahead journaling | `OrderManager` |
| `portfolio` | Realized/unrealized accounting, mark-to-market, snapshots | `PortfolioLedger` |
| `risk` | Pre-trade evaluation, persistent kill-switch, loss monitor | `RiskEngine`, `KillSwitch`, `RiskMonitor`, `RiskViewContext` |
| `results` | Execution performance metrics and artifact report generator | `ResultsWriter`, `compute_max_drawdown` |
| `ops` | Wiring and diagnostics | `open_runtime`, `Runtime`, `DoctorRegistry`, `build_core_checks` |
| `devtools` | Automatic fences | budget, docs, archlint, secretscan, `build_tool_checks` |
| `cli` | The `algodesk` command | Typer app |

## 3. Start-up sequence (`open_runtime`)

1. Find the project root; read `config/platform.toml` and the other layers (platform, profile, run, environment, command line); validate; stop with a three-line error on any problem.
2. Resolve paths; they must stay inside the project folder. Directories are created only when asked.
3. Open the parameter registry (only if its database exists or creation is requested) and load defaults from `config/params/*.toml`.
4. Discover plugin manifests without importing any plugin code.
5. Commands then run; long-running processes (from M1) also open the journal and start logging.

## 4. Where things live

| Path | Content |
|---|---|
| `config/` | `platform.toml`, `profiles/`, `runs/`, `params/*.toml` defaults, `groups/*.toml`, lint and scan rules |
| `data/state/params.db`, `data/state/journal.db` | Parameter overrides and history; journal (git-ignored) |
| `data/state/killswitch.json` | Persistent emergency trading kill-switch state (git-ignored) |
| `data/state/data_manifest.db` | Market data manifest SQLite DB (schema v2: datasets, parts, coverage_days, quarantine) |
| `data/bars/source=S/timeframe=1m/exchange=E/segment=G/symbol=X/adj=A/year=YYYY/YYYY-MM.parquet` | 1-minute historical bars partitioned by year and month (Zstandard compressed) |
| `data/bars/source=S/timeframe=1d/exchange=E/segment=G/symbol=X/adj=A/daily.parquet` | Daily bars stored in a single consolidated file per instrument/adjustment |
| `data/session_bars/date=YYYY-MM-DD/` | Ephemeral live intraday session bars separated from history and purged automatically |
| `data/ticks/exchange=E/segment=S/symbol=X/date=D/` | Hive-partitioned parquet part and compacted day files |
| `data/.quarantine/<timestamp>/` | Quarantined invalid data chunks with validation `report.json` and raw payload |
| `data/.trash/<timestamp>/` | Two-stage soft deletion safety trash awaiting purge |
| `results/<run_id>/` | Session execution artifacts (`SUMMARY.md`, `metrics.json`, `trades.csv`, `equity.csv`, configs) |
| `logs/algodesk.log` | JSON log lines, rotated daily (git-ignored) |
| `.algodesk/conformance.json` | Conformance pass records keyed by plugin code hash |
| `.venv/`, `.local/` | Virtual environment, project-local Python and caches (isolation rule) |
| `_workbench/` | Scratch: task cards, handoff file, design drafts (never in docs) |

## 5. Safety rules already implemented

- An order-capable plugin is refused in research, backtest and paper **before its code is imported**; in live it needs a current conformance record.
- Paper and live loads require a conformance record for the plugin's current code hash.
- Undeclared capabilities raise `NotSupported`; there are no stubs that return success (checked by `algodesk lint`).
- The journal forbids updates and deletes, chains every event by hash, and `journal verify` detects edits, removals and broken links.
- Secrets are references (`secret:<name>`); plain secrets in configuration are rejected; logs and journal payloads pass through the scrubber.
- Parameter changes need a reason; tightening applies at once; loosening while a live strategy is armed needs a typed confirmation and waits for the next session.
- The environment must be isolated: `doctor` and `check.sh` fail if Python, caches or packages come from outside the project folder.

## 6. Plugin loading

`PluginRegistry.create(kind, name, config=..., ctx=...)` checks, in order: the plugin exists; the mode is allowed by its manifest; order-capable plugins only in live (before import); a current conformance record for paper and live; the configuration against the plugin's `ConfigSchema`; then a lazy import under a unique module name, instantiation with `(manifest, config, ctx)`, and a final check that the object satisfies the `Plugin` contract.

## 7. Quality gates (`bash scripts/check.sh`)

isolation, ruff check, ruff format check, mypy (strict), pytest, import-linter contracts, budget (size advisory, dependencies hard), docs check, architecture lint, secret scan. The last line is `CHECK: PASS` or `CHECK: FAIL (n)`.

## 8. Data Architecture (Limiter, Gap Planner, Parallel Fetcher & DataService)

Historical market data flows through a pure planner, central rate limiter, worker fetcher, and transactional bar store:
- **Central Rate Limiter (`RateLimiter`)**: Centralized multi-window sliding limiter enforcing requests-per-second, per-minute, and per-30-minute caps keyed to `(source, endpoint)`. Thresholds are initialized from plugin manifests and can be overridden dynamically in the parameter registry. HTTP 429 or upstream rate errors trigger synchronized backoff across all worker threads with exponential backoff and jitter, logged directly to the journal.
- **Pure Gap Planner (`GapPlanner`)**: Deterministically maps arbitrary requested timeframes to base series `1m` or `1d`. Consults `Calendar` to identify expected trading sessions not marked `COMPLETE`, `CLOSED`, or `UNAVAILABLE`. Requests are chunked within plugin `max_span` and `data.fetch_chunk_days`. Sessions older than plugin retention limits are marked `UNAVAILABLE`.
- **Worker Fetcher (`Fetcher`)**: Thread pool executing chunk fetch plans. For each chunk, the worker acquires the rate limiter, invokes the broker plugin's `history()`, validates candlestick monotonicity and integrity with `BarValidator`, and commits validated bars to `BarStore` or quarantines corrupt records via `QuarantineManager`. Retries up to `data.fetch_retries` before recording `EMPTY` with error details in `fetch_log`. Interrupted fetches cleanly resume from already committed chunks.
- **Unified Query Service (`DataService.get`)**: Unified entrypoint orchestrating planning, rate-limited fetching, Parquet storage retrieval, dynamic backward adjustment, and on-the-fly resampling to the requested timeframe. Supports `download=raw` and `download=adjusted` across `adjust=none|splits|total` (default for raw is `total`). `AdjustmentView` computes backward cumulative adjustment factors and volume divisors across all timeframes (1m, 5m, 1h, 1d, 1w). Refuses series with unresolved `conflict` or `manual_required` actions naming symbol and date; requires `--accept-unverified` to proceed with unverified actions. Returns `DataSlice` with complete provenance (`dataset_ids`, SHA-256 `checksums`, `source`, `adjustment`, `fetched_at`, applied action IDs and hash). `--fill-only` mode fetches and stores data without materializing memory slices. Strategy indicator warm-up is pinned to the run's primary `live_suitable` broker (Upstox), recording explicit overrides to the journal and results manifest.
- **Corporate Actions Architecture (`ActionsStore`, `Reconciler`, `AdjustmentView`)**: Dedicated actions store managing partitioned Parquet datasets (`data/actions/exchange=E/symbol=X/actions.parquet`) tracked in manifest as `kind="actions"`. Independent readers ingest events from Upstox (ISIN-keyed) and yfinance. Multi-source reconciler cross-checks type, ex-date, and ratios, classifying events into `confirmed`, `unverified`, `conflict`, or `manual_required`. Price-based jump detector flags price jumps exceeding `corp.detect_jump_pct` with no action within 1 trading day. Operator overrides in `config/corporate_actions_overrides.toml` strictly take precedence over broker data. Staleness detection triggers full 1d refetch or backward 1m factor update with sample-month audit whenever newer ex-dates arrive.
- **Diagnostic & Audit Tools (`algodesk data actions audit`, `algodesk spike`)**: Audit commands verify derived adjusted series against source-adjusted series within `corp.audit_tolerance_pct`. Diagnostic spikes (`upstox-history`, `yfinance-history`, `upstox-actions`, `compare-adjustment`) compare price ratios and steps around corporate action ex-dates to verify adjustment basis without exposing secrets or tokens.

## 9. Market Sessions & Calendars

Market sessions (open, close, square-off, weekdays) are centralized as parameters under `market.<exchange>.<segment>` in the parameter registry (`config/params/markets.toml`). Calendar plugins read registry parameters for timing while supplying exchange-specific trading days, holidays, per-session closures (closed = all, morning, evening; MCX has two sessions), and special session timings from data files.

## 10. Secrets & Broker Ingestion

Secrets providers (`keychain`, `env`) expose name-based lookup through `SecretReader`. In `build_context`, all retrieved secret values are registered with `Scrubber` to guarantee redaction in logs and journals. Broker plugins implement `DataSource` (historical bars, real-time tick/quote feeds) and `AuthFlow` (interactive browser login or analytics tokens).

## 11. Strategy Runtime, Runner, Replay & Reporting

`SessionRunner` coordinates event streaming, bar building, strategy execution, order routing, risk evaluation, simulated matching, and portfolio ledgering. `StrategyRuntime` provides strategies with a read-only `StrategyContext` (completed bars, incremental indicators, position and portfolio snapshots), dispatches lifecycle hooks (`on_start`, `on_bar`, `on_fill`, `on_timer`, `on_stop`), and isolates strategy exceptions so failed strategies are halted without interrupting the session. `ResultsWriter` produces an 8-file artifact bundle in `results/<run_id>/` (`SUMMARY.md`, `metrics.json`, `trades.csv`, `equity.csv`, `config.effective.txt`, `registry.snapshot.json`, `manifest.json`, and `session.md`). Telegram alerts are delivered out-of-band via `NotifyBridge`.

Run preparation (`algodesk run --prepare`) runs conformance suites for all run plugins, refreshes the instrument master, and runs pre-start doctor checks before execution. Historical replay uses `SimClock` and `ReplayFeed` to replay recorded ticks through the exact production engine wiring. `compare_runs` validates bit-for-bit replay equivalence by checking performance metrics and the ordered journal event sequences (signals, risk decisions, orders, fills). Hermetic test isolation fixture (`tmp_project`) and conftest session guards guarantee tests never mutate repository data or results state.

## 12. Structured Logging & Observability

AlgoDesk Next enforces strict session observability across file logging, write-ahead journaling, console feedback, and post-session audits:
- **File Logging (`logs/algodesk.log`)**: `run`, `replay`, and `feed probe` initialize daily-rotating structured JSON logs via `setup_logging`. All log records pass through `Scrubber` to guarantee credential and token redaction. Log retention is governed by `logs.retention_days`.
- **Session Lifecycle Journal Events (M1 Design 8.1)**: Paper and replay runs record comprehensive execution events into SQLite journal storage: `run.start`, `config.effective`, `registry.snapshot`, `instruments.loaded`, `feed.connected`, per-minute `tick.stats`, `bar.stats`, and `proc.stats` (peak RSS via `resource`, journal bytes, recorder rows and bytes), `signal`, `intent`, `risk.decision`, `order.created`, `order.event`, `fill`, `portfolio.snapshot`, `error`, end-of-run `latency.stats` (tick-to-intent p50 and p95), and `run.end`.
- **Console Status Line**: Periodic status updates emit every `run.status_seconds` (default 30s; silenced with `--quiet`) displaying IST time, state, total ticks, oldest tick age, closed bars, signals, orders, fills, kill-switch status, and RSS.
- **Session Observability Report**: `algodesk session report [RUN_ID]` prints first/last tick in IST, session coverage % against the NSE calendar session, feed gaps, bars per timeframe, signals, orders, fills, rule rejects, p95 latency, peak RSS, recorder rows/bytes, journal growth, and failure reasons. Runs starting after session open are tagged `partial`.

## 13. Not built yet

- **M2 Data layer (remaining)**: Accumulation jobs, BSE and MCX full holiday tables, dynamic adjustments (M2-4).
- **M3 Indicators and screens**: Full built-in set, custom-indicator scaffold, TOML and Python screens, saved screen runs.
- **M4 Backtester and reports**: Backtest mode (bar and tick level), models, portfolio mode, results store, compare.
- **M5 Risk and control**: Risk profiles in the parameter registry, monitors, health guards, Telegram commands, outage policies.
- **M6 Zerodha plugin and shadow**: Zerodha venue (disabled by default), all order types, reconciliation, fee schedules, shadow mode.
- **M7 Remote runner**: Profile B on the static-IP host, login ceremony, probe, supervisor, backups.
- **M8 Extended paper**: Campaign per promoted strategy.
- **M9 Micro-live**: Live gates §8.5, smallest size, supervised execution.
