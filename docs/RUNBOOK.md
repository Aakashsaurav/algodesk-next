# Runbook

Owner: Aakash · Version: 1.0 · Date: 29 October 2026

## Summary

Checklists for working on AlgoDesk Next from the terminal. Always start a terminal with `source scripts/env.sh` so everything stays inside the project folder. Before committing, run `bash scripts/check.sh` and expect `CHECK: PASS`. Run `algodesk doctor` to see the health of the environment (exit 0 all good, 1 warnings, 2 critical). Change adjustable numbers only with `algodesk param set` and a reason. If a check fails, use the table at the end. Live-trading routines are added when the remote runner exists (milestone M7).

## Every terminal session

1. `cd` into the project folder.
2. Source environment script `scripts/env.sh` via `source scripts/env.sh` (prints `ENV: isolated at <folder>`).
3. Prefix every `uv` command with `source scripts/env.sh &&` if unsure.

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
- Verify only bars or ticks: `algodesk data verify --only bars` / `algodesk data verify --only ticks`
- Missing trading day gaps: `algodesk data gaps NSE:EQ:INFY --from 2026-10-01 --to 2026-10-26`

### 8. Spike inspection commands
Probe live/research broker data characteristics without exposing credentials:
- Upstox history reach and split continuity: `algodesk spike upstox-history --symbol RELIANCE --split-date 2026-10-15`
- Yahoo Finance reach and split continuity: `algodesk spike yfinance-history --symbol RELIANCE`
- Upstox corporate actions response fields: `algodesk spike upstox-actions --symbols RELIANCE,INFY`

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

### 12. Paper trading and historical replay

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
