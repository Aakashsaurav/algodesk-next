# Runbook

Owner: Aakash · Version: 1.0 · Date: 29 October 2026

## Summary

Checklists for working on AlgoDesk Next from the terminal. Always start a terminal with `source scripts/env.sh` so everything stays inside the project folder. Before committing, run `bash scripts/check.sh` and expect `CHECK: PASS`. Run `algodesk doctor` to see the health of the environment (exit 0 all good, 1 warnings, 2 critical). Change adjustable numbers only with `algodesk param set` and a reason. If a check fails, use the table at the end. Live-trading routines are added when the remote runner exists (milestone M7).

## Every terminal session

1. `cd` into the project folder.
2. Source environment script `scripts/env.sh` via `source scripts/env.sh` (prints `ENV: isolated at <folder>`).
3. Prefix every `uv` command with `source scripts/env.sh &&` if unsure.

## Fast developer & agent dev-loop

Checklists and tooling to minimize iteration loop latency and context token consumption:

1. **Targeted test iteration with `scripts/t.sh`**:
   - `bash scripts/t.sh [paths]`: Defaults to inspecting `git diff` and mapping modified files directly to relevant test modules (e.g. `src/algodesk/engine` to `tests/core/engine`, or `plugins/brokers/upstox` to `plugins/brokers/upstox/tests`).
   - Runs `pytest -x -q --tb=short`; prints a single `PASS: <summary>` line on success, or `FAIL` and the last 15 lines on error.

2. **Foreground task execution with `scripts/run.sh`**:
   - `bash scripts/run.sh [--timeout <seconds>] <command...>`: Enforces foreground execution with a configurable timeout (default 300s). Captures output and prints the exit code and the last 25 lines, preventing runaway background tasks and token-expensive polling loops.

3. **Fast quality gates with `bash scripts/check.sh --quick`**:
   - Runs `ruff check`, `ruff format --check`, `mypy` on changed Python files only, followed by `scripts/t.sh`.
   - `--summary` mode suppresses passing section noise and prints only failing gates and the final status line.

4. **Instrument master IST daily cache**:
   - Upstox instrument master files (`upstox_master_YYYY-MM-DD.json.gz`) are cached per Indian Standard Time (IST) calendar day in `.local/cache/instruments/`.
   - Reused automatically across `algodesk run --prepare`, `algodesk doctor`, `algodesk spike`, and `algodesk data` commands. Same-day cache hits print `master cached` without re-downloading (~3.4 MB saved per invocation).
   - Use `--refresh-master` on any of these commands (or set `UPSTOX_REFRESH_MASTER=1`) to force a fresh download.

5. **Plugin conformance cache**:
   - `algodesk plugins test --all` compares plugin directory code hashes against recorded passes and reports `skipped (current)` for untouched plugins, cutting test runtime to under a second.
   - Use `algodesk plugins test --all --force` to rerun full test suites for all plugins regardless of status.

6. **Agent handoff rotation**:
   - `bash scripts/handoff.sh add "<CPn done | next | files>"` appends a checkpoint line to `_workbench/handoff.md`.
   - Automatically maintains the 25 most recent lines in `_workbench/handoff.md`, archiving older lines to `_workbench/handoff_archive.md`. Agents read only the last 25 lines (`tail -n 25 _workbench/handoff.md`).

7. **Symbol ranged reads**:
   - `algodesk api index` regenerates `_workbench/API_INDEX.md` with AST-derived `file:start-end` line ranges for every public class, function, and method. Agents locate definitions via grep and perform targeted line-range reads.

8. **Graphify codebase graph**:
   - **Build**: create graph via `graphify .` (or extract code without LLM via `graphify update .`).
   - **Rebuild after commit**: keep the graph current via `graphify update .` (AST-only code re-extraction, zero API cost).
   - **Uninstall**: remove the venv, `.agents/skills/graphify`, and `graphify-out` via:
     ```bash
     rm -rf .local/graphify-venv .agents/skills/graphify graphify-out
     ```

## Before every commit

1. Run quality gates in `scripts/check.sh` via `bash scripts/check.sh`; expect `CHECK: PASS`.
2. Verify configuration in `config/runs/demo_paper.toml` and parameters in `config/params/core.toml`.
3. `git status --short` shows only files you expect (never `data/`, `logs/`, `.local/`, `_workbench/`).
4. Commit and push; AI agents never run `git add`, `commit` or `push`.

## Health check

`algodesk doctor` (add `--verbose` for passes, `--only <id>` for one check, `--json` for machines). Fix every critical item before starting any real run.

## Change a parameter

1. `algodesk param show <prefix>` to see the current value.
2. `algodesk param set <name> <value> --reason "<why>"`. Tightening applies at once.
3. Loosening while a live strategy is armed needs `--confirm "LOOSEN <name>"` and takes effect at the next session start.
4. `algodesk param history <name>` shows the audit trail; `algodesk param reset <name> --reason "<why>"` returns to the default.

## Check the journal

`algodesk journal verify` (hash chain), `algodesk journal tail -n 20`, `algodesk journal runs` (runs that did not close cleanly appear as crashed).

## Kill-switch operations

Emergency trading halt controls backed by atomic state file `data/state/killswitch.json` and mtime checks:
- Activate kill-switch: `algodesk killswitch on --reason "<why>"`
- Inspect kill-switch state: `algodesk killswitch status` (add `--json` for machine output)
- Clear kill-switch: `algodesk killswitch off --reason "<why>"`
- Cross-process effect: any process checking `is_active()` reloads instantly when mtime changes. Every transition is journaled with `durable=True`.


## Upstox authentication & login ceremony

### 1. Analytics token (recommended for backtest/research)
- Generate a 1-year Analytics Token in the Upstox Developer Console.
- Store token securely: `algodesk secrets set upstox_analytics_token`.
- Verify presence: `algodesk login status`.
- Note: Analytics tokens never expire daily, but cannot place live orders.

### 2. OAuth interactive login ceremony
- Ensure credentials exist: `algodesk secrets set upstox_client_id` and `algodesk secrets set upstox_client_secret`.
- Run automated login: `algodesk login upstox`.
- Authorize in browser; redirect hits `http://127.0.0.1:5000/` and stores `upstox_access_token`.
- Manual fallback: `algodesk login upstox --manual` prints the URL and prompts for code.

### 3. Port 5000 note (macOS AirPlay Receiver)
- On macOS Monterey and later, system AirPlay Receiver listens on port 5000 by default.
- If port 5000 is occupied, `algodesk doctor --run demo_paper` warns `upstox.redirect_port in use`.
- To free port 5000: System Settings -> General -> AirDrop & AirPlay -> Turn off "AirPlay Receiver".
- Alternatively, run `algodesk login upstox --manual` which requires no local port.

## Market data management

### 1. Synthetic data generation
Generate deterministic synthetic ticks and quotes for offline testing:
`algodesk data synth --symbols NSE:EQ:HDFCBANK,NSE:EQ:SBIN --days 3 --rows 2000 --seed 1`

### 2. Inspecting coverage
List data files grouped by dimensions:
- By exchange: `algodesk data list --group-by exchange`
- By segment: `algodesk data list --group-by segment`
- Filter by group: `algodesk data list --group demo5`
- As JSON: `algodesk data list --json`

### 3. Deleting market data
Deletion stages files into `data/.trash/<timestamp>/` and records a journal event.
- Dry run: `algodesk data delete HDFCBANK --from 2026-10-01 --dry-run --reason "cleanup"`
- Typed confirmation: `algodesk data delete HDFCBANK --reason "cleanup"` (prompts `DELETE <n> files`)
- Force confirmation: `algodesk data delete HDFCBANK --reason "cleanup" --yes`
- Group delete: `algodesk data delete "@demo5" --dry-run --reason "cleanup"`
- Permanent purge: `algodesk data delete HDFCBANK --purge --reason "immediate"`
Note: Deletions are refused while another process holds an active journal writer lock.

### 4. Safety trash management
- List staged trash: `algodesk data trash list`
- Purge trash: `algodesk data trash purge --yes`

### 5. Compaction
Merge instrument-day part files into single daily parquet files:
- All uncompacted: `algodesk data compact`
- Specific date: `algodesk data compact --date 2026-10-23`

### 6. Fetching historical data
Query historical candlestick bars with automatic gap planning, central rate limiting, and on-the-fly resampling:
- Daily bars (default adjusted): `algodesk data get RELIANCE 1d 2026-10-01 2026-10-20`
- Intraday 1-minute bars: `algodesk data get RELIANCE 1m 2026-10-20T09:15:00 2026-10-20T15:30:00 --source upstox`
- Resampled 5-minute bars: `algodesk data get RELIANCE 5m 2026-10-20 2026-10-22`
- Download unadjusted raw bars: `algodesk data get RELIANCE 1d 2026-10-01 2026-10-20 --download raw`
- Fill cache without rendering table: `algodesk data get RELIANCE 1d 2026-10-01 2026-10-20 --fill-only`

### 7. Status and verification
Inspect datasets, file parts, checksums, and detect trading day gaps:
- Overall status summary: `algodesk data status`
- Grouped status summary: `algodesk data status --group`
- Structured JSON status: `algodesk data status --json`
- Read-only data verification: `algodesk data verify` (checks ticks, bars, checksums, manifest, rules, orphans)
- Verification JSON report: `algodesk data verify --json`
- Adopt orphaned disk files into manifest: `algodesk data verify --adopt`
- Fix and quarantine offending bars (e.g. flat zero-volume phantom bars, undeclared source/timeframe intervals): `algodesk data verify --fix`
- Verify only bars or ticks: `algodesk data verify --only bars` / `algodesk data verify --only ticks`
- Missing trading day gaps: `algodesk data gaps NSE:EQ:INFY --from 2026-10-01 --to 2026-10-26 --source upstox --adj raw` (without `--source`, evaluates and outputs separate blocks for each source with coverage for the timeframe; with `--source`, checks that source only; always prints checked series)

### 8. Spike inspection commands
Probe live/research broker data characteristics without exposing credentials:
- Upstox history reach and split continuity: `algodesk spike upstox-history --symbol RELIANCE --split-date 2026-10-15`
- Yahoo Finance reach and split continuity: `algodesk spike yfinance-history --symbol RELIANCE`
- Upstox corporate actions response fields: `algodesk spike upstox-actions --symbols RELIANCE,INFY`
- Upstox corporate actions raw diagnostic probe (zero tokens disclosed): `algodesk spike upstox-actions --symbols RELIANCE --raw`
- Compare candle adjustment basis across providers: `algodesk spike compare-adjustment --symbol RELIANCE --from 2024-10-01 --to 2024-11-01`

### 9. Quarantine lifecycle
Invalid chunks failing validation are quarantined under `data/.quarantine/<timestamp>/` with `report.json`:
- List quarantined items: `algodesk data quarantine list`
- Inspect report details: `algodesk data quarantine show <id>`
- Release and commit to store: `algodesk data quarantine release <id> --reason "approved manual override"`
- Purge specific item: `algodesk data quarantine purge --id <id> --yes`
- Purge all quarantined items: `algodesk data quarantine purge --all --yes`

### 10. Live session bars
Ephemeral intraday bars stored under `data/session_bars/date=YYYY-MM-DD/`, isolated from history:
- Purge old session partitions (default retention): `algodesk data session-bars purge`
- Purge older than N days: `algodesk data session-bars purge --days 3`

### 11. Instrument groups
- List groups: `algodesk groups list`
- Inspect a group: `algodesk groups show demo5`

### 12. Corporate actions operations
Manage partitioned corporate actions storage (`data/actions/`), reconciliation, operator overrides, and adjustment audits:
- Refresh actions from readers (respects `data.actions_ttl_hours`): `algodesk data actions refresh @demo5`
- Refresh from specific source: `algodesk data actions refresh RELIANCE --source upstox` (options: `nse`, `upstox`, `yfinance`, `all`)
- Refresh from official exchange reference plugin: `algodesk data actions refresh RELIANCE --source nse`
- Compare corporate actions across sources: `algodesk data actions compare RELIANCE` (shows per-event symbol, date, type, sources, values, status, reason)
- List actions and reconciliation statuses: `algodesk data actions list RELIANCE` (add `--raw` for un-reconciled actions)
- Check corporate actions reconciliation status: `algodesk data actions check @demo5` (add `--raw` to inspect raw feeds)
- Add operator override (requires `--reason`): `algodesk data actions override add --symbol RELIANCE --ex-date 2024-10-28 --type bonus --ratio 2.0 --reason "Confirmed via NSE circular"`
- Verify corporate actions against raw Bhavcopy closes: `algodesk data actions verify-prices RELIANCE --from 2024-01-01 --to 2024-12-31`
- Audit derived adjusted series vs source-adjusted series: `algodesk data actions audit RELIANCE --from 2024-01-01 --to 2024-12-31`

### 13. Paper trading and historical replay

Follow the four-step operating lifecycle for every trading session:

#### Step 1: Run preparation
Run preparation executes conformance testing for all plugins required by the run configuration, refreshes the broker instrument master, runs all pre-flight doctor checks, and halts:
```bash
algodesk run demo_paper --prepare
```
Alternatively, test all plugins directly:
```bash
algodesk plugins test --all --run demo_paper
```
If a plugin has not passed conformance or has been edited since its last pass, doctor fails with CRITICAL and prints the exact fix command (e.g. `algodesk plugins test broker upstox`).

#### Step 2: Observation run
Exercise the live market feed, bar building, incremental indicators, and strategy logic without taking on any risk exposure (orders are rejected by RiskEngine with `observe_mode`):
```bash
algodesk run demo_paper --observe
```

#### Step 3: Session day execution
Start the paper trading session during market hours:
```bash
algodesk run demo_paper
```
To inspect execution plan beforehand:
```bash
algodesk run demo_paper --dry-run
```
To run quietly without periodic console status lines:
```bash
algodesk run demo_paper --quiet
```

#### Session day operations checklist
On live paper session days:
1. **Start before 09:10 IST**:
   Ensure the laptop is plugged into AC power. Launch the session with `caffeinate` to prevent macOS system sleep:
   ```bash
   caffeinate -i uv run algodesk run demo_paper_fast
   ```
2. **Do not Ctrl-C**:
   Allow the session to run autonomously until scheduled session stop (approx 15:35 IST). Scheduled shutdown ensures all recorded market events, quotes, and scheduler events align deterministically for exact replay.
3. **Open-at-stop check**:
   After the session concludes, inspect the session report:
   ```bash
   uv run algodesk session report <run_id>
   ```
   Check the "Open at stop" section. Any unresolved position is explicitly itemized with symbol, quantity, and product. No automatic orders are added at stop; verify that intraday positions squared off as expected before market close.

#### Ending a session cleanly (Market close, run.stop_time, or Ctrl-C)
Paper sessions end themselves automatically at market close or can be stopped on operator demand:
1. **Market close automatic shutdown**:
   - The engine queries the exchange calendar registered in `SessionManager` for each universe market.
   - At tradable close plus `engine.session_end_grace_s` (or at configured `run.stop_time`), each market transitions `RUNNING -> CLOSING`.
   - Open resting orders for the market are cancelled through the order manager and venue events are drained.
   - Positions are marked to market from latest quotes.
   - Market transitions `REPORTING -> STOPPED`. When all markets stop, the runner finalizes results (`metrics.json`, `SUMMARY.md`, `events.jsonl`) with `stop_reason="end of session"` and verdict `PASS`.
   - Post-close feed silence is normal and does not trigger feed gap alerts.
2. **Open-hours feed staleness monitoring**:
   - During active trading hours (`RUNNING`), if no tick arrives for `feed.stale_seconds` (default 30.0s), the runner records a `feed.gap` event and alerts via Telegram.
3. **Feed authentication rejection (HTTP 401)**:
   - If the broker WebSocket feed rejects connection with HTTP 401 (e.g. expired daily token), it raises typed `FeedAuthError`.
   - The runner attempts at most one re-authentication from the stored token.
   - If re-authentication fails or is rejected, it avoids reconnect loops, halts new order entries, cancels open resting orders, journals `feed.auth_failed`, sets `stop_reason="feed_auth"` in metrics, and exits cleanly with exit code 1:
     ```
     Feed authentication failed: server rejected WebSocket connection (HTTP 401)
     Where: plugins/brokers/upstox/feed.py
     Fix: run algodesk login upstox
     ```
     (Never outputs "INTERNAL ERROR (bug)").
4. **Graceful operator interrupt (Ctrl-C)**:
   To stop an active paper session at any point, send a single interrupt (`Ctrl-C` / SIGINT):
   - The runner catches `SIGINT` and immediately executes graceful finalization:
     - Recorder flushes in-memory buffer parts and runs compaction into `day.parquet` first, ensuring no ticks are lost or unpersisted.
     - Any open orders are cleanly cancelled through the order manager.
     - Positions are marked using the same latest quotes / synthetic marks as post-session.
     - Results (`metrics.json`, `trades.csv`, `SUMMARY.md`) and session logs are generated cleanly in `results/<run_id>/` with `stop_reason="interrupt"` and verdict `STOPPED`.
2. **Session observability report**:
   ```bash
   algodesk session report [RUN_ID]
   ```
   Inspects IST timestamps, separate p95 latencies (feed delay, platform, and bar-close delay), closed round trips, gross/net P&L, peak RSS, and journal growth.
3. **Replay and compare**:
   Replay the recorded session deterministically and compare paper and replay results:
   ```bash
   algodesk replay --dates YYYY-MM-DD --run demo_paper
   algodesk results compare <paper_run_id> <replay_run_id>
   ```

#### Live-session second terminal monitoring routine
During an active paper trading session, open a second terminal (`source scripts/env.sh`):
1. **Live JSON structured logs**:
   ```bash
   tail -f logs/algodesk.log
   ```
   (Daily rotating, structured JSON with all secrets scrubbed).
2. **Real-time audit journal tail**:
   ```bash
   algodesk journal tail -n 20
   ```
   Follows state transitions, lifecycle events (`tick.stats`, `bar.stats`, `proc.stats`), signals, risk decisions, orders, and fills.
3. **Session observability report**:
   ```bash
   algodesk session report [RUN_ID]
   ```
   Inspects first and last tick (IST), session coverage % against calendar session, feed gaps, closed bars by timeframe, signals, orders, fills, p95 latency, peak RSS, recorder rows and bytes, journal growth, and failure reasons. Runs started after 09:15 IST are labelled `partial`.

#### Step 4: Post-session replay & results comparison
Replay recorded ticks deterministically through the exact engine wiring:
```bash
algodesk replay --dates 2026-10-26 --run demo_paper
```

**Replay a single paper session exactly**:
To reproduce a specific paper run with its exact tick window, initial seed, and warm-up state:
```bash
algodesk replay --session <paper_run_id>
```
This scopes market tick playback strictly to `[first_tick_utc, last_tick_utc]`, restores indicator warm-up from `results/<paper_run_id>/warmup_bars.parquet` (or reconstructs warm-up from the bar store up to the session start), loads the run's recorded configuration (`config.effective.txt`) and parameter snapshot (`registry.snapshot.json`), verifies fee schedule presence, and warns on commit/plugin hash divergence.

Replay dispatches all recorded scheduler inputs (`timer.strategy` such as `exit_time`, `portfolio.snapshot`, `bar.timer_close`, `market.closing`, and session stop) at identical monotonic tick sequence numbers (`seq`). This eliminates wall-clock jitter or SimClock shifts, ensuring 100% equivalence in signals, orders, fills, and metrics (`results compare` returns `equal: True`).

**Replaying one of several sessions on a multi-session day**:
When multiple paper trading sessions are recorded on the same calendar date (e.g. morning and afternoon sessions):
- Each session records its own tick sequence numbers starting from 1.
- Tick recorders store a `session_id` column in partitioned Parquet files, preserved during partition compaction.
- For legacy recordings without a `session_id` column, replay feed isolates ticks using the target session's recorded receive window `[first_tick_recv_utc, last_tick_recv_utc]`, seamlessly continuing past interleaved ticks from other sessions without premature termination.
- If sequence collisions make the playback window ambiguous, replay safely halts with an actionable `DataError`.
- Always replay a specific session on multi-session dates via `algodesk replay --session <paper_run_id> --run <run_name>`.
- Replay never reports `stop_reason="interrupt"` unless an operator actually sends an interrupt signal (`SIGINT`/`SIGTERM`) during the replay run; completing playback of an interrupted paper session records `stop_reason="end of session"`. If feed data ends early before reaching the recorded stop sequence, replay records `stop_reason="replay data ended (<last_tick_time>)"` and verdict `STOPPED`.

**Replay scoping by date (`--dates`)**:
When replaying by date (`algodesk replay --dates YYYY-MM-DD`):
- Candidates are strictly scoped to paper trading sessions; replay result folders are ignored.
- If exactly one recorded session exists for that date, its recorded time window (`first_tick_utc` to `last_tick_utc`) is automatically adopted, avoiding SimClock defaults to 09:15 IST and preventing max drawdown shifts.
- If multiple recorded sessions exist on date `YYYY-MM-DD`, execution is safely refused to prevent mixing tick windows or carrying stale indicator state across sessions. The CLI lists all matching recorded sessions with their run IDs, tick windows, and trade counts. To replay, specify `--session <paper_run_id>`.

Compare performance metrics and ordered event sequences (signals, risk decisions, orders, fills) between paper and replay runs:
```bash
algodesk results compare <paper_run_id> <replay_run_id>
```
On any divergence, `results compare` prints sequence counts (`signals`, `risk_decisions`, `orders`, `fills`) and up to 3 individual differences per sequence with timestamps, instruments, sides, and prices (e.g. `signals[39]: A [ts=..., price=..., inst=..., side=...] != B [ts=..., price=..., inst=..., side=...]`). Subsecond microsecond jitter in timer exit intent IDs and minor float variations in stale quote age reasons are normalized during semantic comparison.

**Dropped signal observability and Rejects by Rule**:
Every signal emitted by a strategy that does not become an order is journaled with the rejecting rule or component (data_integrity stale data, duplicate_entry_guard, oms, etc.) and aggregated into Rejects by Rule in `metrics.json` and session reports. In replay, asynchronous bar tasks are gathered and awaited synchronously (`_drain_pending_bar_tasks`) before subsequent ticks or recorded events are dispatched, advancing `SimClock` monotonically to recorded timestamps so that quote age evaluations match paper execution identically.

**Graceful shutdown and operator interrupts**:
For sessions of 60 minutes or longer, prevent OS system sleep using `caffeinate -i algodesk run demo_paper_fast`.
Stopping an active paper session cleanly via Ctrl-C (`SIGINT` or `SIGTERM`) cancels the feed reader task, cleanly awaits generator closing (`stream.aclose()`), flushes and closes the parquet recorder, cancels open orders, marks to market, and records verdict `STOPPED` with stop reason `interrupt`.
Verify session status and stop reason via:
```bash
algodesk session report <run_id>
```
Which prints:
```text
Verdict:              STOPPED
Stop Reason:          interrupt
```
Verify bit-identical deterministic replay determinism:
```bash
algodesk replay --dates 2026-10-26 --run demo_paper --twice
```
List and view past run result artifacts:
```bash
algodesk results list
algodesk results show <run_id>
```
Test Telegram notifications:
```bash
algodesk telegram test
```

## Corporate actions, audit & data gaps

### Corporate actions refresh & OAuth requirements
Upstox fundamentals corporate-actions requires an active OAuth access token (`upstox_access_token`).
- Refresh actions: `algodesk data actions refresh <symbol>` (continues with other sources if one fails).
- Diagnostic probe: `algodesk spike upstox-actions --symbols <symbol> --raw` (prints token type used).
- If actions are older than `data.actions_ttl_hours` and OAuth token is absent, `algodesk doctor` issues a warning to run `algodesk login upstox`.

### Actions audit & price verification
Audit the computed dividend-adjusted series against vendor total-adjusted downloads:
```bash
algodesk data actions audit <symbol> --from <YYYY-MM-DD> --to <YYYY-MM-DD>
```
Reports max and median percentage differences, highlighting any symbols exceeding the tolerance threshold. If missing a series, prints the exact command to fetch it.

Verify corporate action execution factors against raw Bhavcopy price jumps:
```bash
algodesk data actions verify-prices <symbol> --from <YYYY-MM-DD> --to <YYYY-MM-DD>
```
Evaluates observed raw close factor ($P_{\text{ex}} / P_{\text{prev}}$) against declared event factors and standard nominal corporate action ratios within `corp.price_verify_tolerance_pct` (default 8.0%).

### Inspecting data gaps and phantom bars
Inspect missing trading days across series:
```bash
algodesk data gaps <symbol> --from <YYYY-MM-DD> --to <YYYY-MM-DD> --timeframe 1d --source upstox
```
Flat zero-volume bars (open=high=low=close with volume 0 on expected sessions) are dropped and recorded as `EMPTY` gap days in the manifest when `data.drop_flat_zero_volume_bars = true`.

### Market closure inference & calendar audits
Detect exchange holidays and special trading sessions empirically:
- Infer closures: `algodesk data closures infer --exchange NSE --from <YYYY-MM-DD> --to <YYYY-MM-DD> [--sample <SYMBOLS>] [--dry-run]`
  (Evaluates official Bhavcopy archive presence first for NSE: file exists proves open or special; HTTP 404 on weekday proves closed. Sample symbols decide only when Bhavcopy is unreachable and never override declared calendar special sessions; special sessions without source bars report as "special session, source has no bars", not closures).
- Clear recorded closures: `algodesk data closures clear [--inferred] [--exchange E] [--from <YYYY-MM-DD>] [--to <YYYY-MM-DD>]`
- Audit discrepancies between calendar and data: `algodesk data closures check --exchange NSE [--from <YYYY-MM-DD>] [--to <YYYY-MM-DD>]`
- List recorded closures: `algodesk data closures list --exchange NSE`
- Doctor checks: `algodesk doctor` warns when calendar coverage expiration falls within `calendar.warn_days` (default 60 days) or when unresolved closure disagreements exist.

### Instrument ID migration & contract-aware layout
Migrate legacy tick and bar partition paths (with spaces/raw trading symbols) to canonical structured IDs (`EXCHANGE:SEGMENT:SYMBOL[:EXPIRY[:STRIKE:TYPE]]`):
- Preview migration plan: `algodesk data migrate-ids --dry-run`
- Copy files, verify row counts/checksums, and update manifest: `algodesk data migrate-ids --apply`
- Permanently remove verified legacy source files: `algodesk data migrate-ids --finalize`
- Diagnostics: `algodesk doctor` reports `data.legacy_ids` warning while legacy partition files remain.

### Daily instrument master diffing & universe tracking
Inspect changes between daily master snapshots:
- Compare snapshots: `algodesk instruments diff [DATE_A] [DATE_B] [--source upstox] [--json]`
- Output reports added, removed, and changed instruments (lot size, tick size, expiry, ISIN) and journals `instruments.diff`.
- Universe warnings: changes impacting active universe groups trigger doctor warnings (`instruments.diff_warnings`) and Telegram session alerts.

### Universe resolution & trading feasibility
- Inspect group with frozen content hash: `algodesk groups show <GROUP_NAME> --resolved [--json]`
- Check capital feasibility against latest close prices: `algodesk instruments feasibility --group <GROUP_NAME> --capital <INR> [--json]`
- Feasibility evaluates single lot requirements against capital, noting that margin requirements are not estimated.

## Daily Data Operations Routine

Maintain historical bar datasets and tick archives with this four-step daily operational workflow:

### 1. Login & authentication
Verify or refresh broker authentication for market data feeds:
```bash
algodesk login upstox
```
If using interactive token retrieval, complete the browser loopback flow to persist the access token in keychain/env.

### 2. Pre-run preparation & health verification
Run system diagnostics and verify that market calendar, instrument master, and data stores are sound:
```bash
algodesk doctor
algodesk run --prepare
```
Doctor check `data.accumulation_lag` warns if historical datasets are lagging behind completed trading sessions by more than `data.accumulate_lag_warn_days` (default 3 days).

### 3. Historical data accumulation & coverage reset
Fetch missing completed trading sessions (1m and 1d) for the trading universe, and reset any stale EMPTY coverage records:
```bash
# Preview stale EMPTY coverage records (e.g. from premature fetches or old date windows)
algodesk data coverage reset-empty --source upstox --timeframe 1m --dry-run

# Reset stale EMPTY records to MISSING in manifest and journal (resets premature same-day EMPTY fetches)
algodesk data coverage reset-empty --source upstox --timeframe 1m

# Re-derive coverage_days from stored parquet bars (repairs any EMPTY/PARTIAL regressions)
algodesk data coverage repair --source upstox --timeframe 1m --dry-run
algodesk data coverage repair --source upstox --timeframe 1m

# Preview accumulation plan without network calls
algodesk data accumulate --group demo5 --source upstox --days 5 --dry-run

# Execute accumulation (refetch COMPLETE days only when --refresh is specified)
algodesk data accumulate --group demo5 --source upstox --days 5
```
Reports rows added per instrument and timeframe, refetched days, remaining missing days, and unrecoverable days beyond broker retention windows (e.g. 7 days for yfinance 1m). Uses the shared GapPlanner to evaluate real coverage gaps: when the cache is up to date, it prints "Up to date: no missing completed sessions" and exits 0; days beyond source retention are listed as lost and never planned. For sources configured with `source.<name>.same_day_history = false` (such as Upstox), same-day history is not served by the vendor's historical endpoint during or immediately after the session on the same IST date; fetches on that date record non-final `PENDING` status (reported as `MISSING` by `gaps`), and `data accumulate` marks same-day sessions as waiting ("waiting: same-day history not served") and defers fetching until the next IST date. Premature `EMPTY` records from same-day fetches can be cleared with `data coverage reset-empty`. Furthermore, `data coverage repair` reconstructs coverage from stored parquet files while guarding `CLOSED` days—a `CLOSED` day is never turned into `COMPLETE` unless declared as an exchange special session (e.g. Muhurat trading) or flagged with an inferred-closure disagreement. Loud failure triggers only when chunks were actually fetched without error and zero rows were added for days that are not final EMPTY. Refetches that return zero rows or fail (HTTP 401, 403, 429, timeout) never overwrite a day that has stored bars or COMPLETE status; auth errors raise a typed error naming `run algodesk login <source>` and are never stored as EMPTY. Records `data.accumulate` to the journal.

### 4. Integrity verification, comparison & live smoke test
Verify store integrity and compare against recorded ticks or secondary sources:
```bash
# Verify checksums and parquet partition schemas across datasets
algodesk data verify

# Compare rebuilt 1m bars from recorded ticks against broker 1m bars
algodesk data compare --date 2026-11-09 --symbols RELIANCE,TCS --source yfinance

# Execute live smoke test (owner-run, network)
algodesk data smoke --symbols RELIANCE,TCS
```
`data compare` reconstructs 1m bars in memory from recorded ticks using `BarBuilder` (aligned to the exchange IST session across UTC partitions), closes active boundary bars deterministically, and fetches vendor source 1m bars through `DataService`. The output table reports bar counts (`BUILT`, `SOURCE`, `MATCHED`), first/last bar timestamps in IST (`BUILT FIRST`, `BUILT LAST`, `SRC FIRST`, `SRC LAST`), one-sided bar counts (`BUILT ONLY`, `SRC ONLY`), and price/volume difference metrics. When tick recording starts mid-session, the first built bar's volume difference (which carries cumulative day volume) is excluded from `MAX VOL DIFF` and `MEDIAN VOL DIFF`, marked `MAX VOL DIFF*`, and explained with a footnote. When built bars exist where the vendor source has no bars (e.g. dropped historical minutes), an explicit "SRC MISSING" explanation line is emitted. If either side has no bars, it refuses cleanly with exit code 1 naming which side was empty and why (`no ticks recorded`, `same-day candles are not served by the history endpoint`, `source returned nothing`, or `date beyond the source's retention`), never printing a table of zeros.

`data smoke` verifies that repeated `get` queries execute with zero network calls (100% cache hits) across COMPLETE, CLOSED, or retry-exhausted EMPTY days. In-progress sessions (`CoverageStatus.PARTIAL`) before settlement deadline are excluded from failure checks and listed, and any network calls made on Run 2 are itemized. EMPTY days are final after `data.fetch_retries` until refetched via `algodesk data get ... --refresh`.

### 5. Safe tick archive management & vendor import
Manage tick archives safely:
```bash
# Plan safe deletion of older ticks (requires typed confirmation phrase 'DELETE <N> files')
algodesk data delete NSE:EQ:RELIANCE --kind ticks --older-than 30 --reason "Routine 30d cleanup"

# Ingest vendor historical files (CSV or Parquet) using column mapping
algodesk data import data/vendor/sample.csv --mapping config/importer_mappings/vendor.toml
```
Ticks are irreplaceable: `--purge` is strictly forbidden on ticks, and deleted files are moved to timestamped `.trash/<timestamp>` folders.

### 6. Screens, index lists, constituent history groups, backfill & screen universe
Run screens across market groups, import NSE index lists and constituent histories, resolve point-in-time universe membership, backfill historical data, and run screens as dynamic trading universes:

```bash
# Run screen with explicit adjust basis and execution timing breakdown
algodesk screen run sma_trend_test --group nifty50 --as-of 2026-11-12 --timing

# Inspect previous screen results
algodesk results list

# Import current index constituent CSV (maps ISIN -> symbol in NSE:EQ; creates config/groups/<name>.toml)
algodesk groups import data/vendor/nifty50_current_nse.csv --name nifty50
algodesk groups import data/vendor/nifty500_current_nse.csv --name nifty500 --accept-unmapped

# Import historical constituent timeline (symbols column is authoritative; mismatches written to sidecar JSON)
algodesk groups import-history data/vendor/nifty500_history_constituents.csv --name nifty500_history --index Nifty500
# Fail fatal on any transition inconsistency with --strict:
algodesk groups import-history data/vendor/nifty500_history_constituents.csv --name nifty500_history --index Nifty500 --strict

# Query point-in-time index membership as-of a specific date (prints mapped, unmapped, and coverage %)
algodesk groups show nifty500_history --as-of 2024-03-28
algodesk groups show nifty500_history --as-of 2024-03-28 --resolved

# Backfill daily historical bars for an index group (pure raw fill; never triggers adjustment errors)
algodesk data accumulate --group nifty50 --source upstox --timeframe 1d --days 500 --dry-run
algodesk data accumulate --group nifty50 --source upstox --timeframe 1d --days 500

# Prepare run with screen-as-universe (runs screen on last completed session, freezes hash into universe.frozen)
algodesk run demo_paper --prepare

# Clean orphaned coverage records and unmappable datasets
algodesk data coverage prune --dry-run
algodesk data coverage prune
# Or integrated into data verify:
algodesk data verify --fix
```
Screens default to `adjust = "splits"`: when stored candles match requested basis, no corporate actions are read, preventing false refusals. Screen configurations specify `adjust` explicitly (`splits`, `total`, `none`). Honest skip messages always name the symbol, specific failure reason, and remediation command. `data accumulate` operates strictly in raw mode (`download="raw"`, `adjust="none"`), ensuring missing-bar accumulation never halts on corporate action discrepancies. Non-price actions (buybacks, AGM/EGM) are ignored via `config/corporate_action_purposes.toml`. History imports treat `symbols` as authority for each date, logging mismatches to `<name>.inconsistencies.json`. Run configurations accept `universe.screen = "<name>"`, freezing passed symbols and content hash into the journal at run start.

## If something fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `ISOLATION` or `env.isolated` fails | New terminal without `env.sh` | `source scripts/env.sh`; rebuild `.venv` with `uv sync` if the interpreter path is wrong |
| `DOCTOR` critical on `config.valid` | Bad key or value in a TOML file | Read the three error lines (file, key, fix) |
| `lint` error `hard_coded_limit` | Number assigned to a limit-like name | Move it to `config/params/*.toml` |
| `docs check` error | Missing Summary or stale `docs/INDEX.md` | Add the Summary; run `algodesk docs index` |
| `secrets scan` finding | Secret-like text in a file | Replace with `secret:<name>`; never commit secrets |
| `budget` FAIL | Too many dependencies | Remove one or approve the change |
| Agent ran out of tokens | Work stopped mid-card | Read the last lines of `_workbench/handoff.md` and resume in a new conversation |
