# Status

Owner: Aakash · Version: 1.0 · Date: 23 October 2026

## Summary

Milestone M0 (foundations) is complete and hardened. Task M1-1 (M1 foundations: domain types, protocols, conformance checkers, exchange calendar plugin, central market sessions, deterministic fee engine, and builtin indicators) is done. Next task is M1-2. No live trading exists and none is planned before the gates in the PRD are met.

## Milestones

| M | Name | State |
|---|---|---|
| M0 | Foundations | Done |
| M1 | Walking skeleton | M1-1 done; M1-2 next |
| M2 to M9 | Data layer, indicators and screens, backtester, risk and control, Zerodha plugin, remote runner, extended paper, micro-live | Not started |

## What exists

Plugin contracts and manifests; layered configuration; parameter registry with change rules; plugin registry with gating and conformance records; hash-chained journal; scrubbed JSON logging; `algodesk` command line with `doctor`, `calendar`, and `fees` groups; budget, docs, lint and secret-scan tools; parameter, journal and tooling hardening (R-01 to R-19); domain market and trading types; runtime Protocols and conformance helpers; central market hours; exchange calendar plugin (`nse`, `bse`, `mcx`); deterministic `FeeEngine` and `zerodha_nse` schedule; builtin indicators (`SMA`, `EMA`, `RSI`).

## Open decisions

None blocking M1.

## Next

1. M1-2.
