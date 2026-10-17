---
name: algodesk-plugin-author
description: Use when creating or changing an AlgoDesk Next plugin (broker, venue, fee schedule, indicator, screen, strategy, risk rule, notifier, calendar, secrets, transport).
---
# Author a plugin

## Summary
A plugin is a folder with a manifest, code, tests and a one-page README. It depends only on `algodesk.api`, declares what it can do, and fails loudly for anything else.

## Folder
`plugins/<kind>/<name>/` containing `plugin.yaml` (or `plugin.toml`, per the config-format decision), `README.md` (starts with a Summary), `plugin.py` (the entry class), `config_schema.py` (optional), `tests/`.

## Manifest must state
`name`, `kind`, `version`, `api_version`, `entry`, `capabilities` (true or false each), `modes_allowed`, `order_capable` (true only for real broker venues), `limits`, `requires` (for example static IP), `verified_on` and `sources` for any external fact.

## Rules
1. Import only `algodesk.api`. Never import another plugin.
2. Declare every capability. Undeclared use raises `NotSupported`; never return fake success.
3. Put limits and tunables in the parameter registry via your config schema.
4. External facts (rate limits, fees, quirks) go in the manifest and README with source and date.
5. Tests: fake the network; include the conformance suite for your kind; include failure cases.
6. A venue with `order_capable: true` is never loaded outside live mode.
7. Keep the plugin focused; size is guidance, not a limit.

## Done when
Conformance suite passes, `algodesk plugins check <name>` passes, `algodesk docs check` passes.
