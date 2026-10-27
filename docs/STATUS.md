# Status

Owner: Aakash · Version: 1.0 · Date: 23 October 2026

## Summary

Milestone M0 (foundations) is complete and hardened. Tasks M1-1, M1-2, M1-3, and M1-4 (speed tooling, OMS, portfolio ledger, risk engine, builtin risk rules, and simulated execution venue) are complete. The workbench features deterministic order state machine transitions, write-ahead journaling, pre-trade risk evaluation without short-circuiting, atomic kill-switch controls, and conservative paper execution with trade-through limit fills. No live trading exists and none is planned before the gates in the PRD are met.

## Milestones

| M | Name | State |
|---|---|---|
| M0 | Foundations | Done |
| M1 | Walking skeleton | M1-1, M1-2, M1-3, M1-4 done; M1 in progress |
| M2 to M9 | Data layer, indicators and screens, backtester, risk and control, Zerodha plugin, remote runner, extended paper, micro-live | Not started |

## What exists

Plugin contracts and manifests; layered configuration; parameter registry with change rules; plugin registry with gating and conformance records; hash-chained journal; scrubbed JSON logging; `algodesk` CLI with `doctor`, `calendar`, `fees`, `secrets`, `data`, `groups`, `login`, `feed`, and `killswitch` groups; budget, docs, lint and secret-scan tools; API index generator (`algodesk api index`); parameter, journal and tooling hardening (R-01 to R-19); domain market and trading types; runtime Protocols and conformance helpers; central market hours; exchange calendar plugin (`nse`, `bse`, `mcx`); deterministic `FeeEngine` and `zerodha_nse` schedule; builtin indicators (`SMA`, `EMA`, `RSI`); secrets providers (`keychain`, `env`); `yfinance` market data broker; official `upstox` broker plugin with OAuth loopback, daily instrument master, and Protobuf streaming feed; deterministic `WallClock` and `SimClock`; `Scheduler` with deterministic priority queue; `EventBus` with FIFO re-entrancy and exception isolation; `SessionManager` market state machine; `BarBuilder` with exchange-anchored sessions and boundary grace close; Parquet `Recorder` with disk guards; daily atomic compaction; k-way streaming `ReplayFeed`; SQLite `DataManifest`; `DataCatalog` with multi-stage trash deletion; instrument groups (`config/groups/demo5.toml`); deterministic `synth_market_data`; `OrderManager` state machine with write-ahead journaling and deterministic client refs; `PortfolioLedger` with exact arithmetic and mark-to-market snapshots; `RiskEngine` with non-short-circuiting rules; `KillSwitch` with atomic file persistence and instantaneous cross-process sync; `RiskMonitor` daily-loss trip; `risk_rules/builtin` plugin (9 pre-trade rules); and `venues/sim` plugin (depth matching, latency jitter, trade-through limit fills, failure injection).

## Open decisions

None blocking M1.

