# Status

Owner: Aakash · Version: 1.0 · Date: 27 October 2026

## Summary

Milestone M0 and tasks M1-1 through M1-5 (OMS, portfolio ledger, risk engine, simulated venue, strategy runtime, demo strategy, results reporting, Telegram alerts, and runner) are complete. The workbench features deterministic order state machine transitions, write-ahead journaling, pre-trade risk evaluation without short-circuiting, atomic kill-switch controls, conservative paper execution with trade-through limit fills, and bit-for-bit replay equivalence. No live trading exists and none is planned before PRD gates are met.

## Milestones

| M | Name | State |
|---|---|---|
| M0 | Foundations | Done |
| M1 | Walking skeleton | M1-1..M1-5 done; M1-6 ready |
| M2 to M9 | Data, indicators, backtester, risk, Zerodha, remote, live | Not started |

## What exists

Plugin contracts and manifests; layered configuration; parameter registry; plugin gating and conformance; hash-chained journal; scrubbed JSON logging; full `algodesk` CLI; budget, docs, lint and secret-scan tools; API index generator; domain market and trading types; central market hours and exchange calendar plugin (`nse`, `bse`, `mcx`); `FeeEngine` and `zerodha_nse` schedule; builtin indicators (`SMA`, `EMA`, `RSI`); secrets providers (`keychain`, `env`); `yfinance` and official `upstox` broker plugin with OAuth loopback and Protobuf feed; deterministic clocks, scheduler, and event bus; session manager and bar builder; Parquet recorder, daily compaction, and replay feed; SQLite manifest, data catalog, and trash deletion; instrument groups; `OrderManager` state machine with write-ahead journaling; `PortfolioLedger` with exact arithmetic and mark-to-market snapshots; `RiskEngine` with non-short-circuiting rules; `KillSwitch` atomic persistence; `venues/sim` plugin (depth matching, latency jitter, trade-through limit fills); `SessionRunner` paper and replay coordinator; `StrategyRuntime` with read-only context and fault isolation; `strategies/sma_cross` demo plugin; `notifiers/telegram` plugin with rate-limiting and circuit breaking; `ResultsWriter` 8-artifact bundles; and M1 doctor checks.

## Open decisions

None blocking M1.

