# Status

Owner: Aakash · Version: 1.0 · Date: 23 October 2026

## Summary

Milestone M0 (foundations) is complete and hardened. Task M1-1 (foundations, fee engine, calendar plugin, indicators), Task M1-2 (secrets providers, yfinance DataSource, Upstox AuthFlow/DataSource with Protobuf feed, and market CLI), and Task M1-3 (engine core and market data layer) are complete. No live trading exists and none is planned before the gates in the PRD are met.

## Milestones

| M | Name | State |
|---|---|---|
| M0 | Foundations | Done |
| M1 | Walking skeleton | M1-1, M1-2, and M1-3 done; M1 in progress |
| M2 to M9 | Data layer, indicators and screens, backtester, risk and control, Zerodha plugin, remote runner, extended paper, micro-live | Not started |

## What exists

Plugin contracts and manifests; layered configuration; parameter registry with change rules; plugin registry with gating and conformance records; hash-chained journal; scrubbed JSON logging; `algodesk` CLI with `doctor`, `calendar`, `fees`, `secrets`, `data`, `groups`, `login`, and `feed` groups; budget, docs, lint and secret-scan tools; parameter, journal and tooling hardening (R-01 to R-19); domain market and trading types; runtime Protocols and conformance helpers; central market hours; exchange calendar plugin (`nse`, `bse`, `mcx`); deterministic `FeeEngine` and `zerodha_nse` schedule; builtin indicators (`SMA`, `EMA`, `RSI`); secrets providers (`keychain`, `env`); `yfinance` market data broker; official `upstox` broker plugin with OAuth loopback, daily instrument master, and Protobuf streaming feed; deterministic `WallClock` and `SimClock`; `Scheduler` with deterministic priority queue; `EventBus` with FIFO re-entrancy and exception isolation; `SessionManager` market state machine; `BarBuilder` with exchange-anchored sessions and boundary grace close; Parquet `Recorder` with disk guards; daily atomic compaction; k-way streaming `ReplayFeed`; SQLite `DataManifest`; `DataCatalog` with multi-stage trash deletion; instrument groups (`config/groups/demo5.toml`); and deterministic `synth_market_data`.

## Open decisions

None blocking M1.

