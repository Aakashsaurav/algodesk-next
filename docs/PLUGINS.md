# Writing a plugin

Owner: Aakash · Version: 1.0 · Date: 19 October 2026

## Summary

A plugin is a folder `plugins/<kind>/<name>/` with a `plugin.toml` manifest, `plugin.py`, a README that starts with a Summary, and a `tests/` folder. It imports only `algodesk.api` (plus packages named in its manifest), declares every capability it supports, and fails loudly for anything else. `algodesk new plugin <kind> <name>` generates a valid skeleton; `algodesk plugins test <kind> <name>` runs its tests and base-contract checks and records a conformance pass tied to its code hash. Paper and live modes load only plugins with a current pass.

## Kinds

broker, venue, fees, indicator, screen, strategy, risk_rule, notifier, calendar, secrets, transport, model, report. The interface for each kind is fixed with its first real plugin (from M1); until then plugins use the base contract (`Plugin`, `PluginBase`).

## Manifest (`plugin.toml`)

| Field | Meaning |
|---|---|
| `name`, `kind`, `version` | Identity; folder names must match; semantic version |
| `api_version` | Must equal the core API version |
| `entry` | `"module:Class"`; the module is a file in the plugin folder |
| `capabilities` | Table of capability name to true or false; must not be empty |
| `modes_allowed` | Subset of research, backtest, paper, live |
| `order_capable` | True only for real broker venues; then `modes_allowed` must be exactly live |
| `config_schema` | Optional file defining a pydantic `ConfigSchema`; options given without a schema are rejected |
| `limits`, `requires` | Declared facts (rate limits, static IP, packages) |
| `verified_on`, `sources` | Required whenever the manifest states external facts |

## Rules

1. Import only `algodesk.api`, the standard library, files in your own folder (relative imports) and packages listed in `requires.packages`. Never import another plugin or another core package.
2. Use `PluginBase`; call `require("capability")` before using one; undeclared use raises `NotSupported`.
3. Tunables go through the parameter registry; no numeric limits in code.
4. External facts (limits, fees, quirks) go in the manifest and README with a source and date.
5. Tests never touch the network or real secrets; mark fake secrets with `# fake-secret`.
6. Order-capable plugins are never imported outside live mode; `plugins test` checks them by manifest only.

## Workflow

1. `algodesk new plugin <kind> <name>`
2. Edit `plugin.toml`, `plugin.py`, README Summary, tests.
3. `algodesk plugins check` (discovery problems), then `algodesk plugins test <kind> <name>`.
4. `bash scripts/check.sh` before committing; editing the plugin invalidates its conformance record.
