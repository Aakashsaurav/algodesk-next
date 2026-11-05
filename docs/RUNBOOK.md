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
- Fix and quarantine offending bars (e.g. flat zero-volume phantom bars): `algodesk data verify --fix`
- Verify only bars or ticks: `algodesk data verify --only bars` / `algodesk data verify --only ticks`
- Missing trading day gaps: `algodesk data gaps NSE:EQ:INFY --from 2026-10-01 --to 2026-10-26 --source upstox --adj raw` (reports `MISSING` for unfetched sessions vs `EMPTY` for fetched sessions with 0 rows; defaults to series with most coverage)

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

#### Ending a session gracefully (Ctrl-C)
To stop an active paper session at any point, send a single interrupt (`Ctrl-C` / SIGINT):
1. **Graceful shutdown sequence**: The runner catches `SIGINT` and immediately executes graceful finalization:
   - Recorder flushes in-memory buffer parts and runs compaction into `day.parquet` first, ensuring no ticks are lost or unpersisted.
   - Any open orders are cleanly cancelled through the order manager.
   - Positions are marked using the same latest quotes / synthetic marks as post-session.
   - Results (`metrics.json`, `trades.csv`, `SUMMARY.md`) and session logs are generated cleanly in `results/<run_id>/`.
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
This scopes market tick playback strictly to `[first_tick_utc, last_tick_utc]`, restores indicator warm-up from `results/<paper_run_id>/warmup_bars.parquet` (or reconstructs warm-up from the bar store up to the session start), and verifies configuration and seed alignment.

**Handling multiple recorded sessions on a single day**:
If several paper sessions were recorded on date `YYYY-MM-DD`, running:
```bash
algodesk replay --dates YYYY-MM-DD
```
refuses execution to prevent mixing tick windows or carrying stale indicator/position state across sessions. The CLI lists all matching recorded sessions with their run IDs, tick windows, and trade counts. To replay, pass `--session <paper_run_id>`.

Compare performance metrics and ordered event sequences (signals, risk decisions, orders, fills) between paper and replay runs:
```bash
algodesk results compare <paper_run_id> <replay_run_id>
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

### Actions audit
Audit the computed dividend-adjusted series against vendor total-adjusted downloads:
```bash
algodesk data actions audit <symbol> --from <YYYY-MM-DD> --to <YYYY-MM-DD>
```
Reports max and median percentage differences, highlighting any symbols exceeding the tolerance threshold. If missing a series, prints the exact command to fetch it.

### Inspecting data gaps and phantom bars
Inspect missing trading days across series:
```bash
algodesk data gaps <symbol> --from <YYYY-MM-DD> --to <YYYY-MM-DD> --timeframe 1d --source upstox
```
Flat zero-volume bars (open=high=low=close with volume 0 on expected sessions) are dropped and recorded as `EMPTY` gap days in the manifest when `data.drop_flat_zero_volume_bars = true`.

### Market closure inference & calendar audits
Detect exchange holidays and special trading sessions empirically:
- Infer closures from liquid basket: `algodesk data closures infer --exchange NSE --from <YYYY-MM-DD> --to <YYYY-MM-DD> [--sample <SYMBOLS>] [--dry-run]`
- Clear recorded closures: `algodesk data closures clear [--inferred] [--exchange E] [--from <YYYY-MM-DD>] [--to <YYYY-MM-DD>]`
- Audit discrepancies between calendar and data: `algodesk data closures check --exchange NSE [--from <YYYY-MM-DD>] [--to <YYYY-MM-DD>]`
- List recorded closures: `algodesk data closures list --exchange NSE`
- Doctor checks: `algodesk doctor` warns when calendar coverage expiration falls within `calendar.warn_days` (default 60 days) or when unresolved closure disagreements exist.

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
