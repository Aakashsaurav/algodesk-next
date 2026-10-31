# Status

Owner: Aakash · Version: 1.0 · Date: 30 October 2026

## Summary

Milestone M0 and all M1 tasks (M1-1 through M1-8) are complete. The workbench features deterministic order state machine transitions, write-ahead journaling, pre-trade risk evaluation without short-circuiting, atomic kill-switch controls, conservative paper execution with trade-through limit fills, bit-for-bit replay equivalence, and end-to-end session observability. M1-8 delivers JSON file logging, M1 design 8.1 journal lifecycle events (`proc.stats`, `latency.stats`, `run.start/end`), console status lines, `algodesk session report`, dated Zerodha NSE/BSE fee schedules (2026-04-01 STT notice, options pricing with exercise guard), verified NSE 2026 calendar data, and 16 reference fee vectors. No live trading exists and none is planned before PRD gates are met.

## Milestones

| M | Name | State |
|---|---|---|
| M0 | Foundations | Done |
| M1 | Walking skeleton | Done (M1 acceptance checklist complete) |
| M2 | Data layer | M2 design approved v1.1 |
| M3 to M9 | Indicators, backtester, risk, Zerodha, remote, live | Not started |

## M1 acceptance checklist

- Two live paper sessions with replay and results compare.
- Fee vectors verified by hand.
- Measured numbers: tick-to-intent p95, memory, recorder rows and bytes per day, journal growth.

## What exists

Plugin contracts and manifests; layered configuration; parameter registry; plugin gating and conformance; hash-chained journal; scrubbed JSON logging; full `algodesk` CLI; budget, docs, lint and secret-scan tools; API index generator; domain market and trading types; central market hours and exchange calendar plugin (`nse`, `bse`, `mcx`); `FeeEngine` and `zerodha_nse` schedule; builtin indicators (`SMA`, `EMA`, `RSI`); secrets providers (`keychain`, `env`); `yfinance` and official `upstox` broker plugin with OAuth loopback and Protobuf feed; deterministic clocks, scheduler, and event bus; session manager and bar builder; Parquet recorder, daily compaction, and replay feed; SQLite manifest, data catalog, and trash deletion; instrument groups; `OrderManager` state machine with write-ahead journaling; `PortfolioLedger` with exact arithmetic and mark-to-market snapshots; `RiskEngine` with non-short-circuiting rules; `KillSwitch` atomic persistence; `venues/sim` plugin (depth matching, latency jitter, trade-through limit fills); `SessionRunner` paper and replay coordinator; `StrategyRuntime` with read-only context and fault isolation; `strategies/sma_cross` demo plugin; `notifiers/telegram` plugin with rate-limiting and circuit breaking; `ResultsWriter` 8-artifact bundles; M1 doctor checks; hermetic test isolation fixture (`tmp_project`); conftest session guard; `run --prepare` and `plugins test --all`; `results compare` metrics and event sequence diffing; and safety property test suites.

## Open decisions

None blocking M1.

