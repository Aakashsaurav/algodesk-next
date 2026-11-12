# Status

Owner: Aakash · Version: 1.0 · Date: 30 October 2026

## Summary

Milestones M0, M1, and M2 are complete. AlgoDesk Next provides a pluggable trading workbench for Indian markets with layered TOML configuration, parameter registry audit trails, hash-chained journaling, scrubbed JSON logging, deterministic execution engine, and bit-for-bit replay equivalence. Milestone M2 delivers the historical market data layer: RateLimiter, GapPlanner, Fetcher, BarStore, QuarantineManager, BarValidator, CorporateActions, Bhavcopy reference ingestion, contract-aware partition layout, accumulation, tick-source compare, trash-safe tick deletion, vendor file import plugin, and live smoke tests.

## Milestones

| M | Name | State |
|---|---|---|
| M0 | Foundations | Done |
| M1 | Walking skeleton | Done (M1 checklist complete) |
| M2 | Data layer | Done (M2 checklist complete) |
| M3 to M9 | Indicators, backtest, risk, Zerodha, remote, live | Not started |

## M1 acceptance checklist

- Two live paper sessions with replay and results compare.
- Fee vectors verified by hand.
- Measured numbers: tick-to-intent p95, memory, recorder rows/bytes, journal growth.
- M1-15: Duplicate-entry prevention guard (`prevent_duplicate_entry`), square-off exit time entry restriction, unresolved open-at-stop reporting, unified net-of-charges win rate definition, and Quote-aware exact replay equivalence.

## M2 acceptance checklist

M2 design (section 1.4) success criteria mapped to commands and tests:
1. **Identical get produces zero network calls**: `tests/core/data/test_m2_9_smoke.py`, `algodesk data smoke --symbols RELIANCE,TCS`.
2. **Quarantine & crash resilience**: Corrupt bars quarantined; good data never overwritten; `tests/core/data/test_m2_validator_quarantine_verify.py`.
3. **`data verify` clean on real cache**: `algodesk data verify`, `tests/core/data/test_manifest.py`.
4. **5m/1h/1w rebuilt equals tick-built (FR-DAT-8)**: `tests/core/data/test_m2_9_compare.py`, `algodesk data compare --date 2026-11-04`.
5. **Adjusted view matches source-adjusted; conflict refusal**: `tests/core/data/test_m2_adjusted_views.py`, `algodesk data actions audit RELIANCE --from 2026-01-01 --to 2026-11-01`.
6. **Contract-aware partitions & rollover**: `tests/core/data/test_m2_3b_cp2_layout_migration.py`, `algodesk data migrate-ids --dry-run`.
7. **Quality gates & budget pass**: `bash scripts/check.sh` -> `CHECK: PASS`.

## What exists

Plugin contracts and manifests; parameter registry; append-only journal; scrubbed logging; full CLI; fee engine; order state machine; risk engine; simulated venue; session runner; deterministic replay; market data store, rate limiter, gap planner, corporate actions reconciler, calendar closures, and vendor importer plugin.

## Open decisions

None blocking M2.
