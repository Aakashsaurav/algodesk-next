# Exchange Trading Calendar Plugin

Owner: AlgoDesk Core · Version: 0.1.0 · Date: 2026-10-23

## Summary

The `exchange` calendar plugin provides market session timing, holiday schedules, and special session handling for Indian exchanges (NSE, BSE, and MCX). Operating session hours are read dynamically from central parameters in the registry, while per-exchange holidays, session closures, and special trading windows are loaded from validated TOML data files. The plugin strictly verifies date coverage and raises `CalendarCoverageError` when dates fall outside loaded coverage or when special sessions have unannounced timings.

## Configuration

Configure the exchange via the `exchange` option in `RunConfig` or plugin configuration:

- `exchange`: Exchange code (`"nse"`, `"bse"`, or `"mcx"`). Default is `"nse"`.

## Shipped Data

- `data/nse.toml`: NSE equity, F&O, and currency holiday data for 2026.
- `data/bse.toml`: BSE holiday data for 2026.
- `data/mcx.toml`: MCX commodity holiday and session closure data for 2026 (including morning and evening sessions).
