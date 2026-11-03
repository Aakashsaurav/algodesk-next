# Exchange Trading Calendar Plugin

Owner: AlgoDesk Core · Version: 0.2.0 · Date: 2026-11-03

## Summary

The `exchange` calendar plugin provides market session timing, holiday schedules, and special session handling for Indian exchanges (NSE, BSE, and MCX). Operating session hours are read dynamically from central parameters in the registry, while per-exchange holidays, session closures, and special trading windows are loaded from validated TOML data files. The plugin strictly verifies date coverage and raises `CalendarCoverageError` when dates fall outside loaded coverage or when special sessions have unannounced timings.

## Configuration

Configure the exchange via the `exchange` option in `RunConfig` or plugin configuration:

- `exchange`: Exchange code (`"nse"`, `"bse"`, or `"mcx"`). Default is `"nse"`.

## Shipped Data & Coverage

- `data/nse.toml`: NSE equity, F&O, and currency holiday data for 2022 to 2026 (`coverage_start = 2022-01-01`, `coverage_end = 2026-12-31`). Verified against official NSE circulars Ref 117/2021 (2022), 150/2022 (2023), 154/2023 (2024), 170/2024 (2025), and 2026 schedule (`verified = true`, `verified_on = 2026-11-03`). Includes special sessions: Union Budget live session (2025-02-01), disaster recovery switch sessions (2024-03-02, 2024-05-18), and annual Muhurat trading sessions.
- `data/bse.toml`: BSE holiday data for 2022 to 2026 (`coverage_start = 2022-01-01`, `coverage_end = 2026-12-31`), verified against official BSE holiday notices aligned with the Indian equity market calendar.
- `data/mcx.toml`: MCX commodity holiday and session closure data for 2024 to 2026 (`coverage_start = 2024-01-01`, `coverage_end = 2026-12-31`). Verified against MCX Circular No. MCX/TRD/814/2024 (2025) and published holiday schedules for 2024 and 2026. Note: Years 2022-2023 are unverified from official exchange publications and remain uncovered.
