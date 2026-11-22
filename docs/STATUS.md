# Status

Owner: Aakash · Version: 1.0 · Date: 30 October 2026

## Summary

Milestones M0-M3 complete; M4 in progress. AlgoDesk Next provides a pluggable trading workbench for Indian markets with layered TOML configuration, parameter registry audit trails, hash-chained journaling, scrubbed JSON logging, deterministic execution engine, and bit-for-bit replay equivalence. Milestone M2 delivers the historical market data layer: RateLimiter, GapPlanner, Fetcher, BarStore, QuarantineManager, BarValidator, CorporateActions, Bhavcopy ingestion, accumulation, tick compare, and vendor import.

## Milestones

| M | Name | State |
|---|---|---|
| M0 | Foundations | Done |
| M1 | Walking skeleton | Done (checklist complete) |
| M2 | Data layer | Done (checklist complete) |
| M3 | Indicators and screens | Done |
| M4 | Backtester and reports | In progress (M4-1 to M4-6 complete) |
| M5 to M9 | Risk, Zerodha, remote, extended paper, live | Not started |

## Acceptance checklists

- **M1**: Accepted on recorded paper sessions with bit-identical signals, orders, fills, and metrics; replay tolerances proven safe; fee vectors verified by hand; p95 latency and memory measured.
- **M2**: Zero-network identical get; quarantine resilience; `data verify` clean; rebuilt timeframes equal tick-built; corporate actions audit; contract partitions; quality gates pass.
- **M4**: Multi-instrument backtest engine with exact replay equivalence, tick fidelity, causal determinism, registry governance, point-in-time screen-driven rotation strategy (`screen_rotation`) with two-step rebalance, fill-verified rebalances reporting (`rebalances.csv`), daily mark-to-market equity curve, hardened risk-adjusted metrics, canonical manifest hashing, survivorship disclosure, multiset equivalence with order counting, order-identity calibration pairing, and isolated profile benchmarking with fixed vs per-bar cost separation (ADR-089, ADR-090, ADR-091).

## What exists

Plugin contracts and manifests; parameter registry; append-only journal; scrubbed logging; full CLI; fee engine; order state machine; risk engine; simulated venue; session runner; deterministic replay; market data store, rate limiter, gap planner, corporate actions reconciler, calendar closures, vendor importer; screens runner, ranking, and point-in-time screen rotation strategy with rebalances reporting.

## Open decisions

None blocking.
