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

### Backtest and the kill-switch
1. **State Isolation**: In backtest mode, the kill-switch operates on an isolated, run-local state file inside the run directory (`results/<run_id>/killswitch.json`). A backtest never inspects or modifies `data/state/killswitch.json`: an armed project switch will not halt a backtest, and a breach in a backtest leaves the project switch file byte-identical.
2. **Breach Policy (`backtest.on_kill_switch`)**:
   - `continue` (default): when a daily loss limit or risk rule trips the kill-switch, new entry orders are blocked for the rest of the run, while reducing orders (exits of existing positions) continue to execute. The simulation marks daily positions and completes through the end of historical data with verdict `PASS`.
   - `stop`: the simulation stops immediately upon breach with stop reason `"kill-switch tripped"` and verdict `PASS`.
   - In both policies, `metrics.json` records `kill_switch_tripped_date`, `kill_switch_rule`, and `kill_switch_values`. `SUMMARY.md` displays `"KILL-SWITCH tripped on <IST date>: <rule>, <values>; no new entries after this date"` as its first line. Backtest results omit "Feed halted".
3. **Risk Profile "swing"**: Multi-day and swing strategies configure `risk_profile = "swing"`, with parameters `risk.swing.daily_loss_limit_pct = 4.0`, `risk.swing.weekly_loss_limit_pct = 8.0`, and `risk.swing.drawdown_halt_pct = 15.0`.



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
**Replay after a changed plugin**:
When a strategy plugin or indicator changes after a paper run, its code SHA-256 hash recorded in `.algodesk/conformance.json` changes. Replay checks the loaded plugin against the conformance record and issues a warning if the hash differs from the paper session. If the plugin's iteration order, parameters, or logic changed, replay signals or trades may diverge from paper. To ensure deterministic comparison, replay uses stable instrument ID ascending ordering across all iterations. Re-run conformance testing after editing any plugin via:
```bash
algodesk plugins test strategy <name>
```

**Latency metrics observability**:
- `platform_p95_ms`: Measures platform execution latency from market tick arrival to strategy order intent emission during normal bar processing. Time-of-day timer intents (e.g. 15:15 IST exit_time) do not originate from bar arrivals; their delta from the previous bar close represents holding time, not platform latency, and is measured separately as `timer_latency_p95_ms`.
- `bar_close_delay_p95_ms`: Measures tick-to-bar arrival latency for real market bars. Synthetic gap-fill bars and timer-closed bars (e.g. during illiquid intervals or after silence) are separated into `timer_bar_delay_p95_ms`, preventing multi-minute gaps from distorting live feed delivery metrics.

List and view past run result artifacts:
```bash
algodesk results list
algodesk results show <run_id>
```
Test Telegram notifications:
```bash
algodesk telegram test
```

### 14. Engine backtesting (`algodesk backtest run`)

Execute reproducible historical backtests through the same core engine (`SessionRunner`) using cached historical bars.

#### Step 1: Readiness and cached data
Backtesting operates strictly offline (`cache_only=True`) and never initiates network downloads or connects to live brokers:
```bash
# Verify bars are cached for the target universe and timeframe (e.g. demo5, 1d)
algodesk data catalog --group demo5 --timeframe 1d
```
If bars are missing, accumulate data first:
```bash
algodesk data accumulate --group demo5 --timeframe 1d
```

#### Step 2: Backtest execution plan (--dry-run)
Inspect configuration, parameter snapshot, trading window, and doctor checks without executing:
```bash
algodesk backtest run sma_1d_bt --dry-run
```
Outputs execution plan:
```text
=== Backtest Dry-Run Execution Plan ===
Run Name:          sma_1d_bt
Mode:              backtest
Fidelity:          bar
Timeframe:         1d
Window:            2024-11-10 to 2026-11-13
Allocated Capital: ₹500,000.00
Doctor checks:     0 critical
Plan verified successfully. No network connections opened; no live orders placed.
```

#### Step 3: Run execution
Run the backtest session:
```bash
algodesk backtest run sma_1d_bt
```
Key backtest operational invariants:
- **Zero look-ahead fill model**: Each bar is converted into four synthetic quotes (O, L, H, C for up bars; O, H, L, C for down bars) with depth = `backtest.participation_pct` × volume. Orders decided on bar $t$ fill only on bar $t + \text{delay\_bars}$ (default `backtest.delay_bars = 1`, minimum 1).
- **STOP and STOP_MARKET orders**: Simulated resting stop orders trigger when a synthetic quote crosses `trigger_price`. A stop crossed by the opening quote fills at the open price.
- **Slippage & transaction costs**: Market fills include `backtest.slippage_bps` (default 5 bps). Real statutory taxes, exchange fees, and broker charges are calculated by `FeeEngine` exactly as in paper/live.
- **Batched journaling**: Events write to an isolated per-run journal database (`journal_bt_<run_id>.db`) using batched commits (`backtest.journal_batch_size`), preserving hash-chain integrity while achieving high throughput.
- **Deterministic end of data**: When the historical bar feed exhausts, SimClock advances to the end timestamp of the final trading session. The end-of-session lifecycle runs once: active resting orders are cancelled, portfolio marks are finalized from last quotes, unresolved positions are reported, and results are written with stop reason `end of data` and verdict `PASS`.
- **Idle watchdog (`backtest.max_idle_seconds`)**: If data starvation or deadlocks cause real wall-clock idle time between bar dispatches to exceed `backtest.max_idle_seconds` (default 60.0s), the backtest fails fast with `BacktestWatchdogError`, writes results with stop reason `watchdog` and verdict `FAIL`, and exits CLI with code 1.
- **Progress & Ctrl-C interruption**: Console streams progress to `stderr` every `cli.progress_every` sessions reporting elapsed seconds. Pressing `Ctrl-C` gracefully halts execution, drains venue queues, and writes the results bundle with stop reason `interrupt` and verdict `STOPPED`.

#### Step 4: Inspect results & verify determinism
```bash
# List and inspect results bundle
algodesk results list
algodesk results show <run_id>

# Compare two consecutive runs with identical seed (expected equal: True)
algodesk results compare <run_id_1> <run_id_2>
```

### 15. Backtest fills

In backtest mode, orders execute against synthetic quotes generated by `BarQuoteSynth` under strict causality rules (ADR-085, superseding ADR-079):

1. **Quote path**:
   - For daily bars (`1d` or span $\ge 20$h), `BarQuoteSynth` aligns quotes with exchange calendar session times: Open quote at exchange session open (09:15 IST / 03:45 UTC for NSE), Close quote at exchange session close (15:30 IST / 10:00 UTC), and intermediate quotes (Low/High) evenly spaced within trading hours (`duration / 3`).
   - For up bars ($C \ge O$), path is Open $\to$ Low $\to$ High $\to$ Close.
   - For down bars ($C < O$), path is Open $\to$ High $\to$ Low $\to$ Close.
   - Intraday bars retain their explicit interval period timestamps.

2. **Open-quote fill rule**:
   - Orders decided upon completion of bar $t$ arrive at the timestamp of the Open quote of bar $t+1$.
   - In backtest mode, sim latency defaults to 0 ms (`backtest.sim_latency_ms = 0`). The matching engine evaluates quotes inclusive of arrival time (`quote.recv_ts >= order.arrival`).
   - Market orders fill immediately from the Open quote of bar $t+1$ at `round_to_tick(open(t+1) +/- slippage)`. The fill price and time are completely independent of bar $t+1$'s path direction (zero look-ahead leak).
   - An order decided on bar $t$ can never fill on any quote of bar $t$.

3. **Limit, stop, and slippage**:
   - Limit orders become eligible from the Open quote of bar $t+1$ onward. If marketable against the opening quote, they fill at the open price (with price improvement if the limit is more favorable). If unmarketable, they rest in the sim venue book and execute upon subsequent price trade-through.
   - STOP and STOP_MARKET orders crossed by the opening quote trigger and fill at the open price.
   - `backtest.slippage_bps` is applied symmetrically to market fills (`price * (1 + slippage)` for BUY, `price * (1 - slippage)` for SELL) and rounded to the instrument's tick size.

### Rotation backtest: two-step rebalance and what is not modelled

Screen-driven portfolio rotation backtests (`plugins/strategies/screen_rotation`, ADR-089, ADR-090) operate under specific cash settlement and rebalancing assumptions:

1. **Two-step rebalance execution**:
   - In conservative risk profiles, margin buffer rules (`margin_buffer_pct = 25%`) require cash to remain strictly above the buffer threshold. Concurrent SELL and BUY orders fail because funds from departing position sales have not settled when BUY entries are pre-trade checked by `RiskEngine`.
   - Rebalancing executes in two sequential steps:
     1. **Session close of rebalance day**: Departing positions (held symbols no longer qualifying in top $N$) are sold at session close.
     2. **First completed bar after sell fills**: Once all departing sells fill at market open, replacement BUY orders are emitted and sized strictly from current portfolio equity and free cash (`max(0, cash - min_cash_buffer)`), never sizing from un-freed capital.
   - Reducing orders (SELLs of held long positions) bypass entry-only risk rules (`cash_available`, `max_gross_exposure_pct`, `max_position_pct`, `max_order_value_pct`) to guarantee positions can always be exited safely. The kill-switch retains absolute priority and halts all orders immediately if tripped.

2. **What is not modelled**:
   - **Weights are not re-balanced between rotations**: Positions keep their drifted size across sessions. Between rotation dates, winning positions appreciate while losing positions decline without continuous intra-cycle reweighting or daily trimming.
   - **Survivorship bias**: The backtest uses current index constituent lists (e.g. current Nifty 50) rather than point-in-time historical constituent memberships.
   - **Dividends not credited**: Dividends are not added to the cash ledger; prices are split-adjusted only.
   - **Corporate actions**: Rights issues, spin-offs, and tender offers are unmodelled.

3. **Rebalances artifact (`rebalances.csv`)**:
   - Every rebalance writes a row with 8 columns: `rebalance_date`, `screen_result_hash`, `selected_symbols`, `intended_sold`, `intended_bought`, `executed_sold`, `executed_bought`, `rejected` (`symbol:rule`).
   - The `executed_*` columns are populated strictly from confirmed fills, distinguishing strategy intent from executed reality.


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
Reports rows added per instrument and timeframe, refetched days, remaining missing days, and unrecoverable days beyond broker retention windows (e.g. 7 days for yfinance 1m). Uses the shared GapPlanner to evaluate real coverage gaps: when the cache is up to date, it prints "Up to date: no missing completed sessions" and exits 0; days beyond source retention are listed as lost and never planned. For sources configured with `source.<name>.same_day_history = false` (such as Upstox), same-day history is not served by the vendor's historical endpoint during or immediately after the session on the same IST date; fetches on that date record non-final `PENDING` status (reported as `MISSING` by `gaps`), and `data accumulate` marks same-day sessions as waiting ("waiting: same-day history not served") and defers fetching until the next IST date. Premature `EMPTY` records from same-day fetches can be cleared with `data coverage reset-empty`. Furthermore, `data coverage repair` reconstructs coverage from stored parquet files while guarding `CLOSED` days—a `CLOSED` day is never turned into `COMPLETE` unless declared as an exchange special session (e.g. Muhurat trading) or flagged with an inferred-closure disagreement. When coverage records indicate COMPLETE but no stored bar exists in Parquet, `data coverage repair` sets those days to `MISSING` (journaled with per-symbol counts). When coverage says covered but no bar exists, screen skip messages state `"coverage and bars disagree: run algodesk data coverage repair"`; otherwise they name `data accumulate`. Loud failure triggers only when chunks were actually fetched without error and zero rows were added for days that are not final EMPTY. Refetches that return zero rows or fail (HTTP 401, 403, 429, timeout) never overwrite a day that has stored bars or COMPLETE status; auth errors raise a typed error naming `run algodesk login <source>` and are never stored as EMPTY. Records `data.accumulate` to the journal.

> [!NOTE]
> `data.fetch_max_workers` defaults to 1. At 300 requests per minute the rate limiter, not the worker count, bounds the speed. Running multiple concurrent fetch workers risks SQLite connection contention and race conditions during manifest transaction commits; single-worker operation ensures clean deterministic serial commits.

### 4. Integrity verification, comparison & live smoke test
Verify store integrity and compare against recorded ticks or secondary sources:
```bash
# Verify checksums, parquet partition schemas, coverage-without-bars, and bars-without-coverage across datasets
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

#### Best practices: long runs, cache-only screens & backfill speed

1. **Long runs: redirect to file, do not pipe into head**:
   Always redirect console output from long-running screens or accumulation commands to a log file instead of piping directly into truncation filters like `head`:
   ```bash
   # RECOMMENDED: Redirect to file, then inspect
   uv run algodesk screen run sma_trend_test --group nifty500 --as-of 2026-11-12 --timing > _workbench/screen500.log 2>&1
   tail -20 _workbench/screen500.log

   # AVOID: Piping into head closes stdout prematurely (SIGPIPE)
   uv run algodesk screen run ... | head -20
   ```
   While screen and CLI runners gracefully catch `BrokenPipeError` and guarantee result bundle persistence under `results/run_<run_id>/`, piping into `head` closes stdout early and truncates runtime summaries. Redirecting ensures full diagnostic history.

2. **Cache-only screen execution & dynamic warm-up window**:
   Screens operate strictly offline on local cached Parquet data; they never touch the network or call broker APIs. The read window is computed dynamically from the largest indicator warm-up required by the screen plus `screen.lookback_margin_sessions` (default 20), walking backward across completed calendar trading sessions.
   If a symbol lacks local data, has fewer completed trading sessions than required by indicator warm-up periods, or has a gap inside the window, the runner skips it honestly with explicit reasons:
   - `no_data`: symbol has zero stored bars.
   - `insufficient_history`: symbol has bars but fewer than required warm-up.
   - `gap_in_window`: missing sessions detected within the read window (names the first missing date).
   Remediation instructions name the exact command:
   ```bash
   algodesk data accumulate --group <group> --timeframe 1d
   ```
   `results/<run_id>/table.csv` records all evaluated symbols with an explicit `passed` column (`YES` or `NO`) and evaluated feature values.

3. **Coverage EMPTY finality & reset**:
   `EMPTY` coverage records are final only when the symbol has bars on a later date (pre-listing days) or the empty range spans at most `data.empty_final_max_sessions` sessions (default 5). All-empty ranges and ranges with fetch errors remain non-final (`MISSING`) and are re-evaluated during accumulation.
   Reset existing stale EMPTY records for symbols with zero stored bars using bulk evaluation:
   ```bash
   # Dry-run shows a concise 1-line count summary per reason; use --verbose (-v) for symbol list
   algodesk data coverage reset-empty --source upstox --timeframe 1d --dry-run
   algodesk data coverage reset-empty --source upstox --timeframe 1d --dry-run --verbose

   # Real reset modifies manifest and writes journal entries
   algodesk data coverage reset-empty --source upstox --timeframe 1d
   ```

4. **Screen run skip breakdown & remediation**:
   `algodesk screen run` prints evaluated skip counts by category (`no_data`, `insufficient_history`, `gap_in_window`, `adjustment_refused`) after the verdict line, along with the first 5 skip messages and their exact remediation commands:
   ```bash
   algodesk screen run sma_trend_test --group nifty500 --as-of 2026-11-13
   ```

5. **Backfill speed, decade chunking & loud empty failure**:
   Daily (`1d`) candles support single-request spans up to 3,650 days (10 years) under Upstox API limits. Multi-symbol group backfills display a per-symbol table of rows added and itemize symbols with zero rows and non-final coverage. If zero-row symbols exceed `data.accumulate_max_empty_pct` (default 2%), `data accumulate` fails loudly with exit code 1, listing failing symbols.


## Backtest readiness and fees

Backtests run strictly offline on cached historical data and enforce data readiness, point-in-time universe membership, and trade-date-effective fee schedules before executing strategies.

1. **Pre-run readiness check & shared series resolution**:
   Inspect whether local data satisfies backtest requirements without executing the session:
   ```bash
   # Tabular summary of symbol status, partition warnings, and fee readiness
   algodesk backtest check <run>

   # Machine-readable JSON output (includes readiness report and fees block)
   algodesk backtest check <run> --json

   # Non-destructive test of earlier date windows
   algodesk backtest check <run> --start 2022-01-03 --end 2022-01-07
   ```
   - **Shared series resolution (ADR-084)**: Both `backtest check` and `HistoricalFeed` use the shared resolver `resolve_backtest_series`. When `adjust="splits"` (default) or `"none"`, the preferred partition is `raw` (falling back to `adjusted` if absent). When `adjust="total"`, preferred is `adjusted` (falling back to `raw`).
   - **Non-blocking partition warnings**: Alternate partitions (e.g. stale `adjusted` partitions or other sources like `yfinance`, `nse_bhavcopy`) appear as single informative warning lines per symbol (`"N other series exist, not used by this run"`), never as run failures and without destructive deletion commands. Stale primary series provide non-destructive repair commands (`algodesk data accumulate ...` or `algodesk data coverage repair ...`).
   - If the percentage of missing symbols exceeds `backtest.max_missing_pct` (default 10.0%), `backtest check` and `backtest run` fail closed with exit code 1 and output the exact remediation command:
   ```bash
   algodesk data accumulate --group <group> --source <source> --timeframe <tf> --days <N>
   ```
   Below the limit, missing symbols are skipped, listed with their skip reasons (`no_data`, `missing_sessions`, `pending_empty`, `coverage_without_bars`, `quarantined`, `insufficient_warmup`), and filtered from the effective universe in ops while keeping engine logic unchanged. Backtest results store `readiness.json` in the run output folder.

2. **Universe point-in-time resolution**:
   - `universe.membership = "at_start"` (default): Resolves active group constituents as-of the backtest start date using point-in-time history parquets (`data/universe/<group>.parquet`) and computes a deterministic SHA-256 hash. Additions and removals later in the backtest window are not modelled.
   - `universe.membership = "union"`: Includes all symbols that ever belonged to the group.

3. **Targeted fee schedule readiness & selection**:
   `backtest check` and `backtest run` scope fee schedule verification strictly to the components required by the run (segment, product from strategy params like `CNC` / `MIS`, and universe instruments). Unused components in the schedule (e.g. options MIS components verified only from late 2026) do not block equity runs.
   `backtest check` prints a dedicated "Fees" block displaying:
   `Component | Segment | Product | Earliest Verified | Status (OK / FALLBACK / ERROR)`
   - Inspect applicable rates and components across the entire schedule:
     ```bash
     algodesk fees show <schedule> --as-of YYYY-MM-DD
     algodesk fees show zerodha_nse --as-of 2024-03-31
     algodesk fees show zerodha_nse --as-of 2024-04-01
     algodesk fees show zerodha_nse --as-of 2026-11-13 --json
     ```
   - To extrapolate earliest verified rates across pre-verified trade dates, configure:
     ```toml
     [fees]
     schedule = "zerodha_nse"
     fallback = "earliest"
     ```
     When fallback is used, the total count of fallback calculations is journaled as `fees.fallback_used` in `metrics.json` and highlighted as an assumption caveat in `SUMMARY.md`.

### Fees by date
Fee components in `zerodha_nse` are date-bounded with verified effective ranges derived from official exchange bulletins and broker charge notices:
- **NSE equity transaction charges (`exchange_txn`)**:
  - `2023-04-01` to `2024-03-31`: `0.00325%` (Zerodha bulletin 347967)
  - `2024-04-01` to `2024-09-30`: `0.00322%` (Zerodha bulletin 373933)
  - `2024-10-01` to `2026-10-29`: `0.00297%` (Zerodha bulletin 391488)
  - `2026-10-30` onward: `0.00307%` (Zerodha calculator; BSE delivery is `0.00375%`)
- **Depository Participant (DP) charge**:
  - Delivery SELL DP charge is ₹13.00 (₹15.34 with 18% GST of ₹2.34) effective 2024-10-01, verified against zerodha.com/charges ("₹15.34 per scrip (₹3.5 CDSL fee + ₹9.5 Zerodha fee + ₹2.34 GST)...") and Zerodha bulletin 391488 (composition changed from 5 + 8 to 3.5 + 9.5). Fills prior to 2024-10-01 use `fees.fallback = "earliest"` (applying ₹13.00).
- **Date boundary enforcement**:
  - Fills on dates prior to the earliest verified date (2023-04-01 for equity transaction charges) fail closed with `FeeScheduleError` unless `fees.fallback = "earliest"` is configured in run configuration.
  - When `fees.fallback = "earliest"` is enabled, the earliest verified rate (0.00325% for equity exchange_txn, ₹13.00 for DP) is applied, fallback calculation counts are recorded in `metrics.json` (`fees.fallback_used`), and `SUMMARY.md` assumptions report `"fee rates before <date> use the earliest verified rate"`.
  - Date inspection via `algodesk fees show zerodha_nse --as-of <date>` inspects active components for that specific date; dates before 2023-04-01 output an actionable `FeeScheduleError` advising `fees.fallback = 'earliest'`.

4. **Historical feed adjustments, sessions & fill mechanics**:
   - `adjust = "splits"` (default): Delivers raw candles adjusted for stock splits and bonuses.
   - `adjust = "total"`: Dynamically applies dividend and corporate action factors; fails closed with actionable `DataError` on corporate action conflict or `manual_required` without swallowing errors.
   - **Daily quote synthesis and fill mechanics**: For daily (`1d`) bars, `BarQuoteSynth` synthesizes 4 quotes: Quote 0 (Open, 00:00 IST), Quote 1 (Low/High, 06:00 IST), Quote 2 (High/Low, 12:00 IST), and Quote 3 (Close, 24:00 IST). For contiguous daily bars, Quote 0 shares the exact boundary timestamp with the preceding bar's `BarClosed`. Under `SimVenue`, orders submitted upon bar close ignore quotes where `recv_ts <= trigger_recv_ts`, filling on Quote 1 (e.g. daily low 1258.25 for 9 Jan RELIANCE) plus slippage (5 bps -> 1258.90).
   - **Session date display**: In `trades.csv`, `ts_ist` displays the clean session date (`YYYY-MM-DD 00:00:00 IST`) rather than 06:00 IST, maintaining the exact 10-column schema strictly unchanged.



## Backtest results and metrics

Every historical backtest produces an immutable, comprehensive results bundle under `results/<run_id>/` and updates the SQLite results store (`data/state/results_index.db`).

1. **Artifacts bundle**:
   - `SUMMARY.md`: Concise session summary bounded to 150 words. Features an initial statistical significance warning if trade count is below `report.min_trades` (default 30), leads with out-of-sample performance when `oos_from` is configured, and concludes with an explicit assumptions line.
   - `equity_daily.csv`: Daily mark-to-market equity snapshots (`date`, `equity`, `cash`, `unrealized`, `realized`, `drawdown`, `daily_return`) recording exactly one row per session close without simulated timers.
   - `round_trips.csv`: FIFO matched round trips with entry and exit IST dates (`entry_date`, `exit_date`), quantity, execution prices, and net P&L after all statutory and broker charges.
   - `orders.csv`: Complete history of created orders, states, order types, limit/trigger prices, and fill execution prices.
   - `metrics.json`: Standardized JSON report (`metrics_version: 1`) including CAGR, annualized volatility, Sharpe and Sortino ratios, maximum drawdown and longest duration in days, win rate, expectancy, profit factor, exposure share, and annualized turnover. Supports `in_sample` and `out_of_sample` splits.
   - `manifest.json`: Full provenance documenting the 8-component canonical `result_hash` (universe hash, dataset checksums, corporate actions hash, fee schedule hash, strategy file hash and parameters, parameter registry snapshot hash, model settings, run seed, and code version).

2. **Results store CLI (`algodesk results`)**:
   - **Rebuild index**: Rescans past results folders and populates SQLite index:
     ```bash
     algodesk results index rebuild
     ```
   - **List runs**: Filter past runs by type or study:
     ```bash
     algodesk results list --type backtest --study sma_sweep
     ```
   - **Show run**: Display `SUMMARY.md` followed by a structured performance metrics table:
     ```bash
     algodesk results show <run_id>
     ```
   - **Delete run**: Safely move a run bundle to `results/.trash/<timestamp>/` and remove it from the index without touching the journal:
     ```bash
     algodesk results delete <run_id> --reason "obsolete trial" --yes
     # Or run with --dry-run to simulate deletion
     ```
   - **Compare backtests**: Compare two backtest runs side-by-side with a headline metrics table, bitwise result hash verification, and detection of the first differing manifest input:
     ```bash
     algodesk results compare <run_a> <run_b>
     ```


## Trials and out-of-sample

### Configuring Studies and Out-of-Sample Split
Backtest configurations in `config/runs/<run>.toml` support study grouping and out-of-sample (OOS) performance partitioning:
```toml
[backtest]
start = "2024-01-02"
end = "2026-11-13"
fidelity = "bar"
timeframe = "1d"
study = "rotation_nifty50"
oos_from = "2025-07-01"
```

1. **Study Trial Counting**:
   - When `study` is configured, `SessionRunner` queries the SQLite results index (`ResultsIndex.count_study_trials(study)`) to determine the current trial count before writing the results bundle.
   - The first run of study `rotation_nifty50` receives `trial_number = 1`, the second receives `trial_number = 2`, etc.
   - Both `metrics.json` and `manifest.json` record `"study"` and `"trial_number"`.
   - Inspection commands such as `algodesk results show <run>` or `algodesk results list` do not mutate the index or increment the trial counter.
   - `SUMMARY.md` explicitly includes `trial n of study S` in its assumptions line (bounded within the 150-word budget).

2. **Out-of-Sample Split and Reporting**:
   - Setting `oos_from = "YYYY-MM-DD"` partitions metrics into `in_sample` and `out_of_sample` blocks in `metrics.json` using the identical metric schema (net P&L, trades, closed round trips, win rate, profit factor, max drawdown, Sharpe).
   - `SUMMARY.md` leads with the out-of-sample performance:
     `Out-of-sample (from YYYY-MM-DD): Net P&L: ₹<net>, max DD: <dd>%, trades: <n>.`
   - If an out-of-sample or in-sample window contains zero executed trades, submetrics report zero trades and closed round trips cleanly without errors while still tracking equity trajectory and drawdowns.

3. **Daily-Loss Baseline Behavior in Backtests**:
   - Risk rules such as `daily_loss_limit_pct` measure losses against the start-of-day equity baseline (`_start_of_day_equity`).
   - In backtest mode, the baseline is reset upon detecting a date change on any market event (Quotes or Bars) before opening orders fill, locking in the previous session's closing mark-to-market equity.
   - This ensures that backtest risk evaluation reflects single-day P&L rather than multi-day accumulated drawdowns, preventing spurious kill-switch breaches.
   - Paper and live trading continue relying on the 15:30 IST session close snapshot timer.


## Backtest speed and equivalence

### Incremental Drawdown State Tracking
Backtest and replay portfolio snapshots track equity drawdowns incrementally using `DrawdownTracker` in `algodesk.results.metrics`.
- Maintains running peak equity, current drawdown amount, current drawdown percentage, peak timestamp, and unrecovered duration in $O(1)$ constant time per snapshot.
- Eliminates historical $O(N^2)$ quadratic scanning where snapshot loops re-evaluated full equity history.
- Mathematically bit-for-bit identical to batch computation across random, monotonic, and flat equity trajectories.

### Unchecked Synthetic Quote Construction
When backtesting from historical bars via `fidelity = "bar"`, `BarQuoteSynth.synthesize` generates intraday synthetic quotes (Open, Low, High, Close) using `Quote.create_unchecked` and `Level.create_unchecked`:
- The source bar is validated once upon ingestion.
- Intraday synthetic quote objects bypass repetitive `__post_init__` order book and timestamp revalidation loops.
- External feeds and paper trading feeds continue to enforce full dataclass validation (`Quote(...)`).

### Feed Fidelity and Session Calendar Alignment
`TickBarFeed` reconstructs 1-minute bars from recorded tick streams honoring real paper execution invariants:
- **Calendar Alignment**: Reconstructs `SessionManager` from the recorded run configuration calendars. Ticks for unconfigured exchanges (e.g. MCX commodity futures when only NSE calendars are active) are dropped, exactly matching paper behavior.
- **Sequence-Honoring Timer Closes**: Recorded `bar.timer_close` events are dispatched strictly at their recorded sequence position before processing subsequent ticks. Bars that paper never closed produce no backtest signal, and no synthetic flat bars are fabricated during illiquid minutes with no ticks.

### Equivalence Divergence Classification
When running `algodesk backtest equivalence --session <run_id>`, any signal discrepancies between paper and backtest are classified into one of seven canonical root causes:
1. `bar content differs`: Bar OHLCV or completeness differs between paper and backtest.
2. `bar missing in paper`: Backtest produced a bar for a minute paper omitted.
3. `bar missing in backtest`: Paper produced a bar that backtest omitted.
4. `timer close differs`: Discrepancy caused by timer-closed bar generation.
5. `session start`: Differences arising from session start boundary conditions.
6. `warm-up`: Differences during historical indicator warm-up phase.
8. `unknown`: Unclassified divergence.

### Equivalence: causes and --explain

When validating backtest fidelity against recorded paper sessions (`algodesk backtest equivalence --session <run_id>`), orders and fills differences are matched by order identity `(instrument, side, decision_ts_floored)` and classified into five canonical causes:
- `rejected in backtest (rule)`: Order intent was rejected in backtest by pre-trade risk or data integrity rules (e.g., data_integrity.stale_data triggered when synthetic quotes at scheduled timer events exceed the staleness threshold).
- `no signal`: No matching strategy signal was emitted in backtest due to prior bar or indicator divergence.
- `fill model (backtest unfilled or filled differently)`: Order was created identically in backtest but left unfilled or filled at different size/price by the simulated execution venue.
- `session end`: Order occurred at the boundary of session termination before the backtest loop scheduled execution.
- `unknown`: Difference cannot be classified automatically.

#### Deep Minute Diagnosis with `--explain`
To diagnose minute-level discrepancies on a phone-width screen (strictly $\le 25$ lines):
```bash
algodesk backtest equivalence --session <session_id> --explain <instrument> <HH:MM>
```
Example:
```bash
algodesk backtest equivalence --session demo_paper_fast_paper_20261119_083320_4f3d6a --explain NSE:EQ:INFY 09:51
```
The output displays:
- Ticks in minute $M-1, M, M+1$ (count and price range)
- Paper bar vs backtest bar OHLCV and synthetic flags
- Timer-closed status for paper and backtest
- Strategy signals produced by each run
- Actionable diagnosis explaining whether divergence stemmed from timer close timing, flat bar synthesis, or risk rejection

### Synthetic quote timing

In bar-fidelity backtests, `BarQuoteSynth` generates synthetic quotes from completed OHLCV bars. For intraday bars:
- The open quote is emitted at bar `start_ts` (e.g. `09:44:00.000` UTC).
- Intermediate quotes are spaced strictly inside the bar (for 1m bars at `+20s` and `+40s`).
- The closing quote is placed at bar `end_ts` minus 1 ms (e.g. `09:44:59.999` UTC).

This timing guarantees that whenever a scheduled boundary timer fires (such as intraday strategy exit at `exit_time = 15:15` IST / `09:45:00` UTC), the latest quote age in the risk engine is exactly 1 ms ($\le 1$ ms), strictly within `risk.*.stale_data_seconds = 5.0s`. Legitimate missing bars (where no bar is received for $\ge 60$ s) continue to trigger the data integrity stale data pre-trade order rejection. The order-arrival rule from ADR-085 remains invariant: orders decided at bar close fill from the open quote of the subsequent bar (`bar t+1`), and no fill uses quotes prior to arrival. Daily bars (`1d`) remain invariant with quotes at 09:15 and 15:30 IST.

## Backtest proof, tick fidelity and calibration

### Equivalence Verification
Verify that backtest strategy execution is causal and identical to paper trading on a recorded session:
```bash
algodesk backtest equivalence --session <paper_run_id>
```
This loads recorded configuration, parameter registry snapshot, and warmup bars, reconstructs 1-minute bars using `TickBarFeed` honoring recorded timer close sequence positions, and compares signal sequences. If signals differ, each difference is itemized with its classified cause (`cause: <cause>`), and the command exits code 1 on mismatch.

### Tick Fidelity Backtest
Execute a backtest driven by raw recorded ticks using the paper latency model:
```bash
algodesk backtest run demo_tick_bt --fidelity tick --session <paper_run_id>
```
The output writes a backtest bundle with `mode: backtest` and `fidelity: tick` in `manifest.json` and `SUMMARY.md`.

### Slippage Calibration
Calibrate bar-fidelity fills against tick-fidelity fills:
```bash
algodesk backtest calibrate --session <paper_run_id>
```
Reports per-fill adverse price difference in bps (mean, median, 95th percentile) and net P&L difference per trade, writes `calibration.json` to the backtest results directory, and prints a suggested `backtest.slippage_bps` value without modifying the parameter registry.

### Synthetic Cache Benchmarking
Benchmark runner execution speed, throughput, and memory scaling with profiling:
```bash
algodesk backtest benchmark --symbols 100 --sessions 250 --timeframe 1d
algodesk backtest benchmark --symbols 20 --sessions 20 --timeframe 1m
```
Generates synthetic bar caches in an isolated temporary directory, runs the full runner pipeline, and outputs throughput in bars/s, peak memory in MB, and top 10 functions by cumulative time via `cProfile`.


## Backtest equivalence, calibrate, benchmark: errors and limits

### 1. Robust Error Termination and Results Closure
Any unhandled exception encountered during `backtest run`, `backtest equivalence`, or `backtest calibrate` cleanly finalizes the session run with `verdict = FAIL` and `stop_reason = error`:
- The results directory is written with final metrics, orders, and manifest before exit.
- Lingering active orders are transitioned to `OrderState.CANCELLED` synchronously in memory without queuing un-awaited coroutines on closing loops, eliminating secondary `JournalError: Cannot append event: no active run` warnings.
- The CLI emits a standardized three-line error message:
  ```text
  ERROR: <what went wrong>
  WHERE: <location>
  FIX: <suggested remediation>
  ```
- Exits with returncode 1 without raw stack tracebacks dumped to stderr.

### 2. MCX Commodity Support & Fee Schedule Consistency
Recorded paper sessions containing MCX commodity futures (e.g. `MCX:FUT:GOLD:2026-12-04`) are evaluated identically across paper, backtest equivalence, slippage calibration, and tick fidelity:
- All execution paths use `src/algodesk/fees/mapping.py` `resolve_fee_segment_and_product` to resolve canonical `(segment, product, exchange, instrument_type)`. MCX futures resolve to `segment = "com"`.
- If the configured fee schedule lacks MCX schedule components, charges are recorded as zero without inventing rates, and `SUMMARY.md` assumptions line and `metrics.json` explicitly disclose: `MCX charges not modelled`.
- When MCX charges are modelled (using declared rates from `zerodha.com/charges#tab-commodities`), charges are calculated with non-zero values (brokerage, CTT, MCX txn, SEBI, GST, stamp) and the caveat is removed.

## MCX gold charges (declared rates)
- **Declared rates**: Zerodha rates for MCX non-agri commodity futures (`MCX:FUT:GOLD:2026-12-04`) are declared effective from 2020-01-01 (copied 21 Nov 2026 from `zerodha.com/charges#tab-commodities`). Brokerage 0.03% (cap ₹20 flat), CTT 0.01% (sell), MCX txn 0.0021%, SEBI ₹10/crore, stamp 0.002% (buy), and 18% GST on (brokerage + MCX txn + SEBI).
- **Replay gold divergence on pre-change sessions**: Replaying a session recorded before this card (e.g. `demo_paper_fast_paper_20261116_083920_e829a2` or `...20261119_083320_4f3d6a`) will differ only in gold charges (charges were 0.00 prior to M4-6, whereas replay on current code applies modelled rates).
- **Paper-vs-replay proof**: A strict bit-equivalent paper-vs-replay proof requires recording a fresh paper session on current code.

### 3. Benchmark Bounds and SIGINT Responsiveness
- `algodesk backtest benchmark` execution duration is capped by `backtest.benchmark_max_seconds` (default: 300.0s in `config/params/backtest.toml`). Upon reaching the limit, the benchmark halts cleanly and emits partial throughput and profiling metrics.
- Progress updates are emitted to stderr every `cli.progress_every` sessions (default: 25).
- SIGINT (Ctrl-C) halts the benchmark measurement within 5 seconds, prints a partial report, and leaves no non-daemon threads or orphaned subprocesses behind.

### 4. Rotation Backtest Execution and Verification
A screen-driven portfolio rotation backtest rotates held assets at scheduled intervals (e.g. every 21 trading sessions) into the top $N$ assets ranked by a quantitative screen evaluated point-in-time on the last completed session.

1. **Check data readiness**:
   ```bash
   algodesk backtest check rotation_1d_bt
   ```
   Verifies calendar coverage, price basis (`splits`), fee components (`zerodha_nse` fallback `earliest`), and screen definition across the universe.

2. **Execute rotation backtest**:
   ```bash
   algodesk backtest run rotation_1d_bt
   ```
   The engine advances daily bars through `HistoricalFeed`. At scheduled rebalance intervals, `screen_rotation` invokes `ctx.screen("trend_rank_test")` as of the last completed session date. Holdings departing the top $N$ are liquidated via MARKET CNC sell orders, and incoming targets are entered with target weight allocations.

3. **Inspect rebalances log**:
   ```bash
   head -n 10 results/<run_id>/rebalances.csv
   ```
   Each rebalance records `rebalance_date`, `screen_result_hash`, `selected_symbols` with percentile scores, `bought`, and `sold`.

4. **Verify repeat determinism**:
   ```bash
   algodesk backtest run rotation_1d_bt
   algodesk results compare <run1> <run2>
   ```
   Confirms bit-for-bit equivalence in trades, signals, fills, rebalances, and `manifest.json` `result_hash`.

### 5. Equivalence, Calibration and Benchmark: How to Read Them

#### Reading `backtest equivalence` Output
`algodesk backtest equivalence --session <paper_run_id>` verifies that reconstructed 1m bars from recorded ticks reproduce the exact same trading signals as paper trading.
- **Signals Equal**: Evaluated as a multiset (Counter) of `(timestamp_floored_to_second, instrument, side)`. Ordering of signals occurring within the same second is ignored because real-time tick streaming and sequential feed iteration order ties differently without impacting trading logic.
- **Order Counts**: Counts unique orders using unique `client_ref` identifiers from journal order events (`order`, `order.created`, `intent`), ensuring single orders with multiple lifecycle entries are not double-counted.
- **Diff Rows**: If signals differ, itemized rows are printed under `Only in paper` and `Only in backtest`. Each row includes the timestamp, instrument, side, triggering bar OHLCV, and the bar's source (`tick-built` vs `timer-closed`).

#### Reading `backtest calibrate` Output
`algodesk backtest calibrate --session <paper_run_id>` evaluates execution slippage between reconstructed bar backtests and recorded tick-fidelity paper fills.
- **Order Identity Pairing**: Fills are paired strictly by `(instrument, side, decision_ts_floored)`. Unpaired fills (e.g. from divergent orders) are tracked and listed separately, preventing cross-instrument mispairing.
- **Adverse Slippage**: Signed slippage in basis points where positive values represent worse execution prices for the trader. Mean, median, and 95th percentile metrics are reported for adverse pairs.
- **Refusal Threshold**: If fewer than 10 fill pairs exist, calibration refuses with an explicit diagnostic (`Fewer than 10 fill pairs found`), preventing volatile or misleading calibration from small sample sizes.
- **Suggested Slippage**: Reported as `SUGGESTED backtest.slippage_bps: <val> (advisory)`. The parameter registry is never modified automatically.

#### Reading `backtest benchmark` Output
`algodesk backtest benchmark [--symbols N] [--sessions N] [--timeframe 1d|1m]` measures backtest runner throughput and memory consumption.
- **Cost Separation**: Fixed startup cost (runtime initialization, registry loading, doctor checks, journal setup) and per-bar execution cost (event dispatch, indicator update, order matching) are reported separately by subtracting a baseline run.
- **Internal Time Profile**: cProfile output filters strictly to project code (`src/algodesk` and `plugins`), sorted by internal function time (`tottime`), isolating real execution hotspots rather than asynchronous framework coroutines.


## Power and sleep: how to run a session

Checklist before and during a paper or live session on macOS:

- **Before starting (`--prepare` or `doctor`)**:
  1. Run `uv run algodesk doctor --only ops.power` (or `uv run algodesk run <name> --prepare`).
  2. If warned `on battery (NN%)` or critical below `ops.min_battery_pct` (50%), **plug in the charger**.
  3. If `ops.keep_awake` is `false` and no sleep-prevention assertion is active, **run with `caffeinate -i`** or enable `ops.keep_awake = true` in `config/params/ops.toml`.
- **During a paper or live session**:
  1. When `ops.keep_awake = true`, `algodesk run` automatically spawns `caffeinate -i -w <pid>` and journals `system.keep_awake`.
  2. `SuspendWatcher` compares wall vs monotonic clocks every 1 s; a jump above `engine.suspend_detect_seconds` (30 s) journals `system.suspended`, sends a Telegram alert, and records `suspended_events` / `suspended_seconds` in `metrics.json`.
- **After a session**:
  1. Run `uv run algodesk session report <run_id>` and check the `Suspended:` line.
  2. Run `uv run algodesk results list --type paper` (automatically indexes any new runs without a manual rebuild).

## Backtest speed: what is optimised and what is not

Summary of M4-8 backtest optimisations (ADR-098) and remaining hot-path costs:

- **What is optimised (backtest-only, byte-identical results)**:
  1. **Kill-switch (`KillSwitch(in_memory=True)`)**: No `os.stat()` or disk JSON writes per event in backtest; paper, replay, and live keep full disk sync.
  2. **Intermediate quotes**: Low/high synthetic quotes (`q1`, `q2`) are skipped in bar-mode backtests when the instrument has no resting order and no open position; open and close quotes are always dispatched.
  3. **Batch journal (`backtest.journal_events`, `backtest.journal_batch_size`)**: Only registry-listed events are recorded; writer thread wakeups occur per batch instead of per append; hash chain remains intact (`journal verify` passes).
  4. **Ledger & RiskView**: `PortfolioLedger.mark()` touches only held instruments via `_keys_by_instrument`; `margin_used`, `unrealized_pnl`, and `snapshot()` positions are cached and invalidated on fill or mark of a held instrument.
  5. **Validation & timestamps**: `SimClock` and `Scheduler` skip `_validate_utc_ts` when `type(ts) is datetime and ts.tzinfo is UTC`; `BarQuoteSynth` caches daily timestamps; `BacktestRunner` uses fixed `+05:30` IST offset arithmetic.
- **What is NOT optimised (kept full-fidelity)**:
  - External data ingestion, `Bar(...)` / `Quote(...)` constructors, paper/live/replay execution paths, OMS state machine, risk rule logic, fee engine calculations, and Parquet `table_to_bars` decoding.
- **Benchmarking**:
  - Use `uv run algodesk backtest benchmark --no-profile` for wall-clock throughput without `cProfile` overhead.

## Feed outages: what the engine does and what to check

Checklist for handling and verifying feed outages in live or paper trading:

- **What the engine does on feed stall/disconnect**:
  1. Detects tick inactivity exceeding `feed.stale_seconds` or WebSocket disconnection.
  2. Journals `feed.gap` or `feed.disconnected` and alerts via Telegram (once per outage).
  3. Pauses strategy bar dispatch while feed is down (`_feed_halted = True`).
  4. On reconnect or new tick/quote arrival, clears halt (`_feed_halted = False`) and resumes bar processing immediately.
  5. Catches up overdue bar-timer closes and strategy timers in time order with clock pinned to scheduled due timestamps.
  6. Collapses repeating periodic stats timers (`proc.stats`, `tick.stats`, `strategy.stats`, `portfolio.snapshot`) to 1 event per kind instead of burst storms.
- **What to check after an outage**:
  1. Run `uv run algodesk session report <run_id>`:
     - Check `Feed Gaps: N` to verify gap detection.
     - Check `Strategy: bars handled N, signals N, errors N` to confirm bars continued processing after the gap.
  2. Inspect journal events: `uv run algodesk journal tail <run_id> --kind feed.gap,feed.connected,strategy.stats`.
  3. Verify deterministic parity: `uv run algodesk replay --session <run_id> --run <run_name>` then `uv run algodesk results compare <run_id> <replay_run_id>`.

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
