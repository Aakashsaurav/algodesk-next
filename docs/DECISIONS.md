# Decisions

Owner: Aakash · Version: 1.0 · Date: 29 October 2026

## Summary

Short decision records (context, decision, reason) in the order they were taken. They explain why the code looks the way it does. Each stays under 80 words; larger designs live in the PRD. Add a new record whenever a choice changes structure, tooling or a rule; never edit history, add a record that supersedes it.

## Records

**ADR-001 Python 3.13 managed by uv.** Mature, widely supported, portable to any server; uv pins the same interpreter everywhere. 3.14 revisited at M7.

**ADR-002 TOML for configuration, manifests, parameter defaults and fee schedules.** Built into Python 3.13, no extra package, comments allowed.

**ADR-003 Local isolation.** Python, libraries, caches and hook environments live under `.venv/` and `.local/`; `uv` is configured for manual Python downloads; `doctor` and `check.sh` verify it.

**ADR-004 Per-kind plugin interfaces are deferred.** They are fixed with each kind's first real plugin so they follow real needs, not guesses.

**ADR-005 `pydantic` with strict models for configuration and manifests.** Unknown keys are errors; messages name the file and key.

**ADR-006 SQLite (WAL) for parameters and journal.** Zero cost, one file each, transactional, ships with Python.

**ADR-007 Hash-chained, append-only journal.** Tamper evidence is cheap now and hard to add later; durable events are synced before returning.

**ADR-008 Shared `Scrubber` in `api`.** Journal and logs must not import each other; both mask secrets through one class.

**ADR-009 Conformance records keyed to code hash.** Editing a plugin invalidates its pass; paper and live need a current pass.

**ADR-010 Parameter changes: tighten at once, loosen gated.** Loosening while a live strategy is armed needs a typed confirmation and waits for the next session.

**ADR-011 Size numbers are advisory guidance.** Quality, documentation and speed come first; dependency counts and structural rules are hard.

**ADR-012 Developer tools live in `src/algodesk/devtools/`.** A top-level folder is not importable from the installed package.

**ADR-013 One module per task card, with checkpoints and a handoff file.** Fewer, larger cards; work can resume with another AI model.

**ADR-014 Zerodha is a venue plugin; brokers are data sources.** The Zerodha plugin also holds its own login flow.

**ADR-015 yfinance is the default research data source, never a live source.** Live and paper need an explicit `live_suitable` source (Upstox); yfinance only by a recorded override.

**ADR-016 Static IP host decided at M7.** Oracle Always Free is the lead candidate; AWS credits are the fallback; no account is created before then.

**ADR-017 Size guidance raised to 20,000 core lines, 100 modules, 2,500 per plugin.** M0 alone was 5,445 lines (about three times the early estimate) because validation, messages and tests were written carefully; size stays advisory.

**ADR-018 Shipped parameter defaults are authoritative.** The database stores overrides and history only; a changed default never breaks startup.

**ADR-019 Parameter and journal writes are transactional and fail closed.** A journal write failure stops all further appends until reopened.

**ADR-020 Public API now holds market and order types; CB-6 raised to ~60 types.** As M1 introduces canonical market and trading data representations across plugins and core, `algodesk.api` defines frozen domain types (Instrument, Quote, Bar, Order, Fill, Position, Portfolio) and Protocols. CB-6 guidance is raised from ~25 to ~60 public exports to accommodate these types while keeping logic out of `api`.

**ADR-021 Market hours are central registry parameters; sessions are data, MCX has two.** Session opening, closing, square-off times and weekdays are defined in `config/params/markets.toml` under the parameter registry, enabling runtime adjustment. Calendar plugins provide exchange-specific trading day status, holidays, and per-session closures (MCX supports morning and evening sessions).

**ADR-022 Fees are dated data read by one engine.** Fee schedules are TOML data files specifying dated component rates (flat, percentage, capped, per-crore, DP per-scrip-day) and GST rules. A single core `FeeEngine` computes charges deterministically across backtest, paper, and live modes rather than writing custom fee code per broker.

**ADR-023 Upstox thin client on httpx, websockets and protobuf over official SDK.** Upstox distributes an official `upstox-python-sdk`, but installing it pulls legacy dependencies (`uuid==1.30`) containing Python 2 syntax (`32L`) that fail immediately with SyntaxError on Python 3.13. We implement a focused thin client directly on `httpx`, `websockets`, and compiled official Protobuf frames. This eliminates 20+ transitive dependencies, works cleanly on Python 3.13, and gives full deterministic control over reconnection backoff and 5-level market depth parsing.

**ADR-024 Injected Clock protocol and ban on direct datetime.now.** To guarantee deterministic replay in backtesting and simulation, modules in engine, oms, risk, portfolio, results, and strategies must never call `datetime.now()` or `time.time()`. Time is accessed strictly through an injected `Clock` protocol (`WallClock` in live/paper, `SimClock` driven by historical events in replay). Enforced by archlint rule `direct_clock`.

**ADR-025 Partitioned Parquet storage layout for tick data with day compaction.** Market events are stored partitioned by `exchange=E/segment=S/symbol=X/date=D/` using zstd compression. Streaming recording writes numbered part files every flush interval without blocking trading; at session end, compaction combines parts into `day.parquet` sorted by `(exch_ts, recv_ts)` and verifies checksums. File metadata is tracked transactionally in SQLite `data_manifest.db`.

**ADR-026 Two-stage data deletion with safety trash directory.** Market tick data cannot be redownloaded. Deletion moves files to a timestamped safety trash directory (`data/.trash/<timestamp>/`) with metadata audit logs, purged only after `data.trash_days` or with explicit `--purge`. Deletions require typed confirmation and refuse execution if the journal lock is held.

**ADR-027 Order state machine, write-ahead journaling and deterministic client refs.** The order manager drives order states strictly through `check_transition`, refusing unapproved intents (INV-3) and logging transitions as `OrderEvent`. To prevent phantom orders, `intent` and `order.created` are journaled with `durable=True` before submission to the execution venue. Client references are deterministic (`<run_id>-<counter>`).

**ADR-028 Pre-trade risk evaluation without short-circuiting and persistent kill-switch.** `RiskEngine` evaluates all configured risk rules without early exit, recording all compared numbers in the journal for full auditability. Reducing intents bypass entry-only rules. The emergency kill-switch persists in `data/state/killswitch.json` and checks mtime on every call for instantaneous cross-process trading halts.

**ADR-029 Conservative simulated execution venue with trade-through limit fills.** `venues/sim` uses no network or clock, pricing orders against the first quote received at or after arrival. Market orders walk depth, cancelling unfilled remainder. Resting limit orders fill only when the opposite market trades through the limit by `sim.through_ticks` ticks, eliminating optimistic queue-priority assumptions.

**ADR-030 Observe mode fail-closed semantics.** Observation mode (`algodesk run --observe`) exercises the entire live pipeline (WebSocket feed, bar aggregation, indicators, strategy evaluation) while guaranteeing no order creation by rejecting all intents at the RiskEngine level with an automatic rejection decision.

**ADR-031 Results bundle eight-file schema.** Every session run produces an immutable directory under `results/<run_id>/` with eight standard artifacts: `SUMMARY.md`, `metrics.json`, `trades.csv`, `equity.csv`, `config.effective.txt`, `registry.snapshot.json`, `manifest.json`, and `session.md`.

**ADR-032 SNTP clock offset and pre-flight doctor checks.** Accurate order timestamps and boundary-aligned bar closing require synchronized clocks. Pre-flight doctor checks query SNTP for sub-second clock offset, verify 30-day calendar coverage, ensure Telegram reachability, and confirm zero order-capable credentials.

**ADR-033 Isolated journal database per test worker and replay session.** Replay sessions and concurrent pytest-xdist workers use isolated journal paths to avoid SQLite flock contention on `journal.db.lock` while preserving strict write-ahead logging and deterministic replay equivalence.

**ADR-034 Run preparation and plugin conformance gating.** `algodesk run --prepare` tests every plugin specified in the active run configuration, refreshes the broker instrument master, and executes pre-flight doctor checks before any orders or feeds start. Doctor check `plugins.conformance` fails closed on missing or stale conformance records with explicit fix instructions.

**ADR-035 Results comparison and event sequence equivalence.** `algodesk results compare` validates replay determinism by comparing summary metrics and the exact ordered event sequence (signals, risk decisions, orders, fills) extracted from journal events. Replay `--twice` guarantees bit-identical reproduction across runs.

**ADR-036 Core modules budget limit guidance raised to 150.** To accommodate the complete M1 walking skeleton implementation across OMS, portfolio accounting, pre-trade risk, results reporting, and engine runtime components, `budget.core_modules_guidance` is raised to 150 modules.

**ADR-037 Base timeframes 1m and 1d only, others rebuilt.** The data layer stores only canonical 1m (intraday) and 1d (daily) base bars in Parquet partitions. All intermediate and higher timeframes (5m, 15m, 1h, 1w) are deterministically rebuilt on the fly from these base bars, eliminating redundant storage and timeframe synchronization skew.

**ADR-038 Live-session bars separate from history and removed daily.** Intraday bars constructed during live or paper streaming sessions are held in separate live-session storage isolated from authoritative historical series. At session completion and compaction, live-session bars are reconciled and cleared daily to prevent duplicate writes or contamination of official historical Parquet datasets.

**ADR-039 Warm-up from the run's live_suitable source (Upstox), other sources only by recorded override.** Strategy and indicator warm-up data must be fetched directly from the run's primary `live_suitable` broker source (Upstox) to ensure exact symbol, tick-size, and pricing continuity with live stream events. Alternative research sources (such as yfinance) are permitted for warm-up only via an explicit, journal-recorded configuration override.

**ADR-040 Quarantine deleted after 3 days via central parameter.** Invalid, unparseable, or non-monotonic data files moved to quarantine are automatically deleted after 3 days. This retention window is governed by a central parameter in the registry, granting operators sufficient time to inspect data defects while preventing unbounded disk accumulation.

**ADR-041 yfinance auto_adjust=True by default, raw optional, corporate actions stored.** The yfinance broker plugin requests historical bars with `auto_adjust=True` by default so strategies receive dividend- and split-adjusted OHLCV. Raw unadjusted prices remain selectable via explicit configuration, and corporate actions (dividends and stock splits) are extracted and persisted alongside price series in the catalog.

**ADR-042 Core rate limiter keyed to (source, endpoint).** Historical and market data requests are throttled centrally in `RateLimiter` using sliding windows across 1-second, 1-minute, and 30-minute intervals. The limiter is keyed per `(source, endpoint)`, pulling limits from plugin manifests and registry parameters. On 429 or rate errors, all workers for that endpoint pause with exponential backoff and jitter, logged directly to the journal.

**ADR-043 M1 acceptance criteria requires two live sessions of 60+ minutes continuous feed.** Formal acceptance for Milestone 1 requires successfully executing at least two live trading sessions of 60 continuous minutes or longer without feed gaps, process crashes, or state corruption. This validates end-to-end WebSocket stability, heartbeat monitoring, and deterministic journaling under genuine market conditions before proceeding to larger real-capital operations.

**ADR-044 actions reconciliation (two sources agree, overrides win, conflict refuses).** Corporate actions (splits, bonuses, dividends, rights) are captured per ISIN and symbol from two independent readers (Upstox and yfinance) and stored in partitioned Parquet files (`data/actions/exchange=E/symbol=X/actions.parquet`). An event is marked `confirmed` when two sources agree on type, ex-date, and ratio (cash dividends within `corp.dividend_tolerance_pct`), `unverified` when reported by only one source, and `conflict` when sources disagree or ratios conflict with price movement. Operator overrides in `config/corporate_actions_overrides.toml` require `--reason` and strictly take precedence over all broker data. Adjusted views refuse to compute factors if any action is in `conflict` or `manual_required` state, failing closed with an explicit error naming the symbol and date. Series with `unverified` actions are refused unless `--accept-unverified` is provided. Upstox candle adjustment basis is tracked as `unknown` until verified by comparison spikes, preventing premature assumption of dividend reinvestment adjustment.

**ADR-045 adjustment basis model (supersedes raw-plus-actions in ADR-041).** Empirical evidence proves yfinance Close and Upstox daily candles are split- and bonus-adjusted (`splits` basis), not raw; only yfinance `Adj Close` adjusts dividends (`total` basis). Dataset manifests track explicit `adjustment_basis` (`none`, `splits`, `total`). `AdjustmentView` converts by basis using confirmed actions: `splits -> total` applies dividends; `splits -> none` reverses splits/bonuses; `total -> *` refuses closed. Default read adjustment is `total`. Provenance records basis, factor kinds, action IDs, and hash.

**ADR-046 core line guidance raised to 25000 in config/params.** As Milestone 2 expanded with corporate actions reconciliation, price anomaly detection, multi-source rate limiting, and empirical basis conversion, the codebase surpassed the earlier 20,000 line advisory limit. Parameter `budget.core_lines_guidance` in `config/params/core.toml` is raised to 25,000 core lines. Size remains advisory guidance behind correctness, typing, and test rigor.




