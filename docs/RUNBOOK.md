# Runbook

Owner: Aakash · Version: 1.1 · Date: 26 November 2026

## Summary

Checklists for operating AlgoDesk Next from the terminal. Always start with `source scripts/env.sh` so Python, caches, and state stay inside the project folder. Before committing, run `bash scripts/check.sh` and expect `CHECK: PASS`. Run `algodesk doctor` for environment health (exit 0 OK, 1 warn, 2 critical). Change adjustable numbers only via `algodesk param set` with a reason. Never run backtest, replay, data, or kill-switch commands as an AI agent on the real repository; the owner runs them.

## Terminal & Dev Loop

1. `cd` into the project root and run `source scripts/env.sh` (prints `ENV: isolated at <folder>`).
2. Prefix `uv` commands with `source scripts/env.sh &&` when unsure.
3. Targeted tests: `bash scripts/t.sh [paths]` (maps `git diff` to tests, runs `pytest -x -q --tb=short`).
4. Foreground timeout wrapper: `bash scripts/run.sh [--timeout <seconds>] <command...>`.
5. Fast gates: `bash scripts/check.sh --quick` (`--summary` for failures only); full gates before commit: `bash scripts/check.sh`.
6. Health & parameters:
   - `algodesk doctor` (`--verbose`, `--only <id>`, `--json`)
   - `algodesk param show <prefix>` | `algodesk param set <name> <value> --reason "<why>"` (`--confirm "LOOSEN <name>"` if loosening while armed) | `algodesk param history <name>` | `algodesk param reset <name> --reason "<why>"`
7. Journal & indexes:
   - `algodesk journal verify` | `algodesk journal tail -n 20` | `algodesk journal runs`
   - `algodesk api index` (updates `_workbench/API_INDEX.md`); `graphify update .` (AST-only graph update); `bash scripts/handoff.sh add "<CPn done | next | files>"`
8. Before commit: `bash scripts/check.sh` -> `CHECK: PASS`; `git status --short` clean of `data/`, `logs/`, `.local/`, `_workbench/`. Agents never run `git add/commit/push`.

## Daily Session Routine (Paper & Replay)

1. **Auth**: `algodesk login status`; `algodesk secrets set upstox_analytics_token` (or `upstox_client_id` + `upstox_client_secret`, then `algodesk login upstox` / `--manual` if port 5000 is used by macOS AirPlay Receiver).
2. **Prepare**: `algodesk run demo_paper --prepare` (runs conformance, refreshes daily IST-cached master in `.local/cache/instruments/`—use `--refresh-master` to force—and runs doctor). Or `algodesk plugins test --all --run demo_paper` (`--force` to bypass hash cache).
3. **Observe (no risk)**: `algodesk run demo_paper --observe`.
4. **Execute before 09:10 IST**:
   - Inspect plan: `algodesk run demo_paper --dry-run`; quiet mode: `algodesk run demo_paper --quiet`.
   - Start on AC power: `caffeinate -i uv run algodesk run demo_paper_fast`. Do not `Ctrl-C`; let scheduled close (~15:35 IST) finish cleanly.
   - Monitor in second terminal: `tail -f logs/algodesk.log`, `algodesk journal tail -n 20`, `algodesk session report [RUN_ID]`.
   - HTTP 401 on feed halts new entries, cancels open orders, and exits 1 with `algodesk login upstox`.
   - `Ctrl-C` (`SIGINT`) flushes recorder parts, compacts `day.parquet`, cancels open orders, marks to market, and writes `verdict = STOPPED`, `stop_reason = interrupt`.
5. **Post-session**:
   - `uv run algodesk session report <run_id>` (check `Open at stop`, `Feed Gaps`, `Suspended:`, `platform_p95_ms`, `bar_close_delay_p95_ms`).
   - `algodesk replay --session <paper_run_id>` (or `algodesk replay --dates YYYY-MM-DD --run demo_paper [--twice]`; multi-session dates require `--session`).
   - `algodesk results compare <paper_run_id> <replay_run_id>` (expects `equal: True`).
   - `algodesk telegram test`.

## Backtest Routine

1. **Readiness**: `algodesk backtest check <run>` (`--json`, `--start YYYY-MM-DD --end YYYY-MM-DD`). Checks coverage, `backtest.max_missing_pct` (10%), and targeted fee components.
2. **Dry-run & Execute**: `algodesk backtest run sma_1d_bt --dry-run`, then `algodesk backtest run sma_1d_bt`.
3. **Verify determinism**: Run twice and `algodesk results compare <run_1> <run_2>` (`equal: True`, hashes match).
4. **Tick fidelity**: `algodesk backtest run demo_tick_bt --fidelity tick --session <paper_run_id>`.

## Backtest Readiness and Fees

- **Series resolution**: `resolve_backtest_series` prefers `raw` when `adjust = "splits"` (default) or `"none"`, and `adjusted` when `adjust = "total"`. Alternate partitions emit non-blocking warnings.
- **Universe**: `universe.membership = "at_start"` (default) resolves constituents as of start date; `"union"` includes all historical members.
- **Fees (`zerodha_nse`)**:
  - Inspect: `algodesk fees show zerodha_nse --as-of YYYY-MM-DD [--json]`.
  - NSE `exchange_txn`: `0.00325%` (2023-04-01..2024-03-31), `0.00322%` (2024-04-01..2024-09-30), `0.00297%` (2024-10-01..2026-10-29), `0.00307%` (2026-10-30+).
  - Delivery DP: ₹13.00 (₹15.34 with 18% GST) from 2024-10-01.
  - MCX non-agri commodity futures (`com:NRML`/`com:MIS`, declared 2020-01-01, copied 2026-11-21): brokerage 0.03% cap ₹20, CTT 0.01% sell, MCX txn 0.0021%, SEBI ₹10/cr, stamp 0.002% buy, 18% GST.
  - Pre-verified dates fail closed with `FeeScheduleError` unless `[fees] fallback = "earliest"` (recorded in `metrics.json` `fees.fallback_used` and `SUMMARY.md`).

## Quote Path and Fills

- **Quote path**: `BarQuoteSynth` emits 4 quotes per bar: `O -> L -> H -> C` ($C \ge O$) or `O -> H -> L -> C` ($C < O$), depth = `backtest.participation_pct` × volume. Daily bars align to 09:15–15:30 IST; intraday closing quotes sit at `end_ts - 1ms` so boundary timers see $\le 1\text{ms}$ quote age (within `stale_data_seconds = 5.0s`).
- **Open-quote fill rule**: Orders decided on bar $t$ arrive at the Open quote of bar $t+1$ (`backtest.delay_bars >= 1`, `backtest.sim_latency_ms = 0`, `quote.recv_ts >= order.arrival`). Market orders fill at `round_to_tick(open(t+1) +/- backtest.slippage_bps)` (default 5 bps), independent of bar $t+1$ direction. Resting `STOP`/`STOP_MARKET` orders crossed at open fill at open; limit orders rest until traded through.
- **Errors & watchdog**: `backtest.max_idle_seconds` (60s) trips `BacktestWatchdogError` (`verdict = FAIL`); unhandled errors finalize results cleanly with 3-line error output.

## Equivalence

Verify paper vs reconstructed 1m bar-fidelity backtest:
```bash
algodesk backtest equivalence --session <paper_run_id>
algodesk backtest equivalence --session <paper_run_id> --explain <instrument> <HH:MM>
```
- **Signals**: Multiset `(ts_floored_s, instrument, side)`. Classified into 7 causes: `bar content differs`, `bar missing in paper`, `bar missing in backtest`, `timer close differs`, `session start`, `warm-up`, `unknown`.
- **Orders/Fills**: Paired by `(instrument, side, decision_ts_floored)` and classified into 5 causes: `rejected in backtest (rule)`, `no signal`, `fill model (backtest unfilled or filled differently)`, `session end`, `unknown`.
- `--explain` prints a $\le 25$-line minute-level diagnosis (ticks in $M-1..M+1$, bars, timer-closed flags, signals).

## Calibration and Benchmark

- **Slippage calibration**: `algodesk backtest calibrate --session <paper_run_id>` pairs bar vs tick fills by order identity (requires $\ge 10$ pairs), writes `calibration.json`, and prints `SUGGESTED backtest.slippage_bps` (advisory only).
- **Benchmark**: `algodesk backtest benchmark --symbols 100 --sessions 250 --timeframe 1d --no-profile` (or `--symbols 20 --sessions 20 --timeframe 1m`). Bounded by `backtest.benchmark_max_seconds` (300s); `Ctrl-C` halts within 5s.
- **M4 optimisations (byte-identical)**: `KillSwitch(in_memory=True)` in backtest, skipping L/H quotes when flat with no resting orders, filtered batch journaling (`backtest.journal_events`, `backtest.journal_batch_size`), `PortfolioLedger` held-instrument mark/cache, O(1) `DrawdownTracker`, unchecked synthetic quotes, and UTC/IST fast-paths.

## Trials and Out-of-Sample

- Configure `study = "<name>"` and `oos_from = "YYYY-MM-DD"` under `[backtest]` in `config/runs/<run>.toml`.
- `SessionRunner` increments `trial_number` from `ResultsIndex.count_study_trials(study)` (read-only commands do not increment).
- `metrics.json` splits `in_sample` and `out_of_sample` from `oos_from`; `SUMMARY.md` ($\le 150$ words) leads with OOS metrics and includes `trial n of study S` in the assumptions line.
- Daily-loss baseline (`_start_of_day_equity`) resets on date change before opening fills in backtest mode.

## Rotation Backtest

```bash
algodesk backtest check rotation_1d_bt
algodesk backtest run rotation_1d_bt
head -n 10 results/<run_id>/rebalances.csv
```
- **Two-step rebalance**: Sells departing symbols at session close; emits replacement BUYs on the first bar after sell fills settle, sized from free cash (`max(0, cash - min_cash_buffer)`). Reducing orders bypass entry-only risk rules.
- **What is not modelled**: Intra-cycle weight drift rebalancing, historical index constituent changes (uses current list), dividends, and rights/spin-offs/tenders.

## Data Routine

1. **Fetch & Accumulate**:
   - `algodesk data get RELIANCE 1d 2026-10-01 2026-10-20 [--download raw] [--fill-only]`
   - `algodesk data get RELIANCE 1m 2026-10-20T09:15:00 2026-10-20T15:30:00 --source upstox`
   - `algodesk data accumulate --group demo5 --source upstox --days 5 [--dry-run]` (`data.fetch_max_workers = 1`; same-day Upstox 1m defers as `PENDING`).
   - `algodesk data synth --symbols NSE:EQ:HDFCBANK,NSE:EQ:SBIN --days 3 --rows 2000 --seed 1`
2. **Coverage & Verify**:
   - `algodesk data list [--group-by exchange] [--group demo5] [--json]`
   - `algodesk data status [--group] [--json]` | `algodesk data gaps NSE:EQ:INFY --from 2026-10-01 --to 2026-10-26 [--source upstox] [--adj raw]`
   - `algodesk data verify [--only bars] [--adopt] [--fix] [--json]`
   - `algodesk data coverage reset-empty --source upstox --timeframe 1m [--dry-run] [--verbose]`
   - `algodesk data coverage repair --source upstox --timeframe 1m [--dry-run]` | `algodesk data coverage prune [--dry-run]`
   - `algodesk data compare --date 2026-11-09 --symbols RELIANCE,TCS --source yfinance`
   - `algodesk data smoke --symbols RELIANCE,TCS` (owner-run, network)
3. **Actions, Closures & Maintenance**:
   - `algodesk data actions refresh @demo5` (or `RELIANCE --source upstox`; Upstox requires OAuth token)
   - `algodesk data actions compare RELIANCE` | `algodesk data actions list RELIANCE [--raw]` | `algodesk data actions check @demo5 [--raw]`
   - `algodesk data actions override add --symbol RELIANCE --ex-date 2024-10-28 --type bonus --ratio 2.0 --reason "..."`
   - `algodesk data actions verify-prices RELIANCE --from 2024-01-01 --to 2024-12-31` | `algodesk data actions audit RELIANCE --from 2024-01-01 --to 2024-12-31`
   - `algodesk data closures infer --exchange NSE --from 2026-10-01 --to 2026-10-31 [--dry-run]` | `algodesk data closures check --exchange NSE` | `algodesk data closures list --exchange NSE` | `algodesk data closures clear --inferred`
   - `algodesk data compact [--date YYYY-MM-DD]` | `algodesk data session-bars purge [--days 3]`
   - `algodesk data delete HDFCBANK --reason "..." [--dry-run] [--yes] [--purge]` (`--purge` forbidden on `--kind ticks`) | `algodesk data trash list` | `algodesk data trash purge --yes`
   - `algodesk data import <file> --mapping <toml>` | `algodesk data migrate-ids --dry-run` (`--apply`, `--finalize`)
   - `algodesk groups list` | `algodesk groups show <name> [--as-of YYYY-MM-DD] [--resolved] [--json]`
   - `algodesk instruments diff [DATE_A] [DATE_B]` | `algodesk instruments feasibility --group <name> --capital <INR>`
   - `algodesk spike upstox-history` | `algodesk spike yfinance-history` | `algodesk spike upstox-actions` | `algodesk spike compare-adjustment`

## Screens

1. Run cache-only screen (redirect long output to file, never pipe to `head`):
   `uv run algodesk screen run sma_trend_test --group nifty50 --as-of 2026-11-12 --timing > _workbench/screen.log 2>&1`
2. Import index lists / history:
   - `algodesk groups import data/vendor/nifty50_current_nse.csv --name nifty50 [--accept-unmapped]`
   - `algodesk groups import-history data/vendor/nifty500_history_constituents.csv --name nifty500_history --index Nifty500 [--strict]`

## Results

- `algodesk results index rebuild`
- `algodesk results list [--type backtest] [--study <name>] [--limit 20]` (auto-indexes new runs)
- `algodesk results show <run_id>` | `algodesk results compare <run_a> <run_b>`
- `algodesk results delete <run_id> --reason "<why>" [--dry-run] [--yes]` (moves to `results/.trash/`)

## Kill-Switch

- `algodesk killswitch on --reason "<why>"` | `algodesk killswitch status [--json]` | `algodesk killswitch off --reason "<why>"`
- Backtests isolate state in `results/<run_id>/killswitch.json` and never touch `data/state/killswitch.json`.
- `backtest.on_kill_switch = "continue"` (default) blocks new entries after breach while allowing reducing exits; `"stop"` halts immediately.
- Swing strategies use `risk_profile = "swing"` (`daily_loss_limit_pct = 4.0`, `weekly_loss_limit_pct = 8.0`, `drawdown_halt_pct = 15.0`).

## Power and Sleep

1. Before paper/live: `uv run algodesk doctor --only ops.power`; plug in charger if on battery or below `ops.min_battery_pct` (50%).
2. Enable `ops.keep_awake = true` (spawns `caffeinate -i -w <pid>`) or run via `caffeinate -i`.
3. `SuspendWatcher` checks wall vs monotonic clock every 1s; jumps $> \text{engine.suspend\_detect\_seconds}$ (30s) journal `system.suspended`, alert Telegram, and record `Suspended:` in `session report`.

## Feed Outages

On tick inactivity (`feed.stale_seconds`) or disconnect, the engine journals `feed.gap`/`feed.disconnected`, alerts Telegram, halts strategy bar dispatch (`_feed_halted = True`), resumes on reconnect, catches up overdue timers in order, and collapses repeating stats timers. Check `algodesk session report <run_id>` and `algodesk journal tail -n 20` (`feed.gap`, `feed.connected`, `strategy.stats`).

## Known Limits

1. **Survivorship bias**: Backtests use current index constituent lists rather than historical point-in-time membership.
2. **Split-adjusted prices**: Stored historical prices are split-adjusted at source, not raw unadjusted prices.
3. **Dividends not credited**: Dividends are not added to the cash ledger in backtests.
4. **Historical fee rates**: Rates verified on 2026-11-20/21 are declared constant for earlier dates via `fees.fallback = "earliest"`, not independently verified per historical date.
5. **MCX charges**: Commodity futures charges are declared from `zerodha.com/charges` (2026-11-21), not independently verified against historical contract notes.
6. **1m backtest speed**: 1-minute backtests are slow (~11 minutes for 10 symbols x 1 year, ~1.8 hours for 100 symbols x 1 year, calculated from 20x20 at ~707 us/bar).
7. **Flat-bar equivalence**: Bar-fidelity equivalence is not exact on flat zero-volume bars at `exit_time` (e.g., 25 Nov 1/123 signals, 19 Nov 4 signals).
8. **Persistent kill-switch trips**: Daily loss trips persist and stop new entries until explicitly cleared.
9. **Paper fills optimistic**: `venues/sim` models trade-through without queue priority or market impact.

## If Something Fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `ISOLATION` / `env.isolated` fails | Terminal missing `env.sh` | `source scripts/env.sh`; rebuild `.venv` with `uv sync` if needed |
| `DOCTOR` critical on `config.valid` | Bad key or value in TOML | Read the three error lines (file, key, fix) |
| `lint` error `hard_coded_limit` | Number assigned to limit-like name | Move to `config/params/*.toml` |
| `docs check` error | Missing/long Summary or stale `INDEX.md` | Fix Summary ($\le 150$ words); run `algodesk docs index` |
| `secrets scan` finding | Secret-like text in file | Replace with `secret:<name>`; never commit secrets |
| `budget` FAIL | Too many dependencies | Remove dependency or approve change |
| Agent ran out of tokens | Stopped mid-card | Read `tail -n 25 _workbench/handoff.md` and resume |
