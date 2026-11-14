# Writing a plugin

Owner: Aakash · Version: 1.0 · Date: 29 October 2026

## Summary

A plugin is a folder `plugins/<kind>/<name>/` with a `plugin.toml` manifest, `plugin.py`, a README that starts with a Summary, and a `tests/` folder. It imports only `algodesk.api` (plus packages named in its manifest), declares every capability it supports, and fails loudly for anything else. `algodesk new plugin <kind> <name>` generates a valid skeleton; `algodesk plugins test <kind> <name>` runs its tests and base-contract checks and records a conformance pass tied to its code hash. Paper and live modes load only plugins with a current pass.

## Kinds

Plugins implement domain protocols defined in `algodesk.api.protocols` (re-exported at `algodesk.api`):

| Kind | Protocol / Interface | Purpose |
|---|---|---|
| `broker` | `AuthFlow`, `DataSource` | Broker authentication and market data ingestion |
| `venue` | `ExecutionVenue` | Order routing, paper execution, or live exchange venue |
| `fees` | `schedule() -> Schedule` | Exchange/broker fee rates and transaction tax schedules |
| `indicator` | `Indicator` | Batch (`compute`) and streaming (`update`) indicators |
| `screen` | `Screen` | Universe filtering and cross-sectional ranking (`evaluate`) |
| `strategy` | `Strategy` | Signal generation and lifecycle (`on_bar`, `on_quote`) |
| `risk_rule` | `RiskRule` | Pre-trade risk assessment (`check`) |
| `notifier` | `Notifier` | Operational messaging and alerts (`send`) |
| `calendar` | `Calendar` | Trading days, sessions, holidays, and special sessions |
| `secrets` | `SecretsProvider` | Secret resolution (`get`, `names`) |
| `transport` | Transport interface | Low-level network connections |

## Shipped Plugins

| Plugin | Kind | Capabilities | Description |
|---|---|---|---|
| `secrets/keychain` | `secrets` | `get`, `set`, `delete`, `names` | macOS Keychain provider via system `security` CLI; maintains a names-only index under `.algodesk/`. |
| `secrets/env` | `secrets` | `get`, `names` | Environment variable provider (`ALGODESK_SECRET_<NAME>`); read-only, `set`/`delete` raise `NotSupported`. |
| `brokers/yfinance` | `broker` | `history`, `actions`, `fundamentals`, `info`, `options` | Historical equity data from Yahoo Finance with NSE/BSE mappings, raw/adjusted OHLCV, and rate limits. |
| `brokers/upstox` | `broker` | `auth`, `instruments`, `history`, `stream` | Official Upstox v2/v3 thin client for OAuth/analytics auth, daily instrument master, history, and Protobuf feed. |
| `calendars/exchange` | `calendar` | `holidays`, `sessions`, `special_sessions` | Exchange trading calendar providing market session timing, holiday schedules, and special session handling for NSE, BSE, and MCX. |
| `fees/zerodha_nse` | `fees` | `equity_delivery`, `equity_intraday`, `futures` | Verified dated fee and tax schedule for Zerodha trading on NSE across delivery, intraday, and equity futures. |
| `indicators/builtin` | `indicator` | `sma`, `ema`, `rsi`, `atr`, `vwap`, `bollinger`, `macd`, `rolling_hl`, `volume_avg`, `roc` | Built-in mathematical indicators supporting batch calculation and streaming updates with golden vectors. |
| `screens/sma_trend_py` | `screen` | `default` | Reference Python screen evaluating SMA(50) > SMA(200) across daily completed bars. |
| `risk_rules/builtin` | `risk_rule` | `pre_trade`, `limits`, `capital_checks` | Pre-trade risk rule plugin evaluating 9 safety controls (cash, exposure, orders/min, daily loss) from registry. |
| `venues/sim` | `venue` | `market_orders`, `limit_orders`, `depth_matching`, `failure_injection` | Simulated execution venue with latency, seeded jitter, depth walking, trade-through limit fills, and failure drills. |
| `strategies/sma_cross` | `strategy` | `signals`, `lifecycle`, `intraday_exit` | Demo dual moving-average crossover strategy exercising bar processing, warmup, signals, and exit timers. |
| `notifiers/telegram` | `notifier` | `alerts`, `heartbeats`, `rate_limiting` | Asynchronous Telegram operational notifier with leaky-bucket rate limiting, circuit breaking, and retry jitter. |

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
