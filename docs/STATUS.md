# Status

Owner: Aakash · Version: 1.0 · Date: 19 October 2026

## Summary

Milestone M0 (foundations) is complete except its last task: the core is built, tested and fenced by automatic checks. The next work is the M1 design document, then the walking skeleton: a first paper session on live Upstox data with a demo strategy. No live trading exists and none is planned before the gates in the PRD are met. Updated at every milestone.

## Milestones

| M | Name | State |
|---|---|---|
| M0 | Foundations | Tasks T0-1 to T0-8 done; T0-9 (docs and acceptance run) in progress |
| M1 | Walking skeleton | Design document next |
| M2 to M9 | Data layer, indicators and screens, backtester, risk and control, Zerodha plugin, remote runner, extended paper, micro-live | Not started |

## What exists

Plugin contracts and manifests; layered configuration; parameter registry with change rules; plugin registry with gating and conformance records; hash-chained journal; scrubbed JSON logging; `algodesk` command line with `doctor`; budget, docs, lint and secret-scan tools. About 5,400 core lines and 600 tests at the end of T0-8.

## Open decisions

- Size guidance numbers (core lines and modules) need re-baselining after M0; see `docs/DECISIONS.md` ADR-011.
- Static IP host: decided at M7 (ADR-016).

## Next

1. Finish T0-9 and the M0 acceptance run.
2. Write the M1 module design (at least five pages) for approval.
3. Issue the first M1 task cards.
