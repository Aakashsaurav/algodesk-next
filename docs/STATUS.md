# Status

Owner: Aakash · Version: 1.0 · Date: 20 October 2026

## Summary

Milestone M0 (foundations) is complete and hardened after an independent code review. The core is built, tested and fenced by automatic checks. The next work is the M1 module design (at least five pages) for approval, then the walking skeleton: a first paper session on live Upstox data with a demo strategy. No live trading exists and none is planned before the gates in the PRD are met.

## Milestones

| M | Name | State |
|---|---|---|
| M0 | Foundations | Done |
| M1 | Walking skeleton | Design document next |
| M2 to M9 | Data layer, indicators and screens, backtester, risk and control, Zerodha plugin, remote runner, extended paper, micro-live | Not started |

## What exists

Plugin contracts and manifests; layered configuration; parameter registry with change rules; plugin registry with gating and conformance records; hash-chained journal; scrubbed JSON logging; `algodesk` command line with `doctor`; budget, docs, lint and secret-scan tools; parameter, journal and tooling hardening (R-01 to R-19).

## Open decisions

None blocking M1.

## Next

1. M1 design document.
2. First M1 task cards.
