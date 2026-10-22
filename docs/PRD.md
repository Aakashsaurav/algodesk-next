# AlgoDesk Next — Product Requirements Document v2.9 (Platform)

Owner: Aakash · Version: 2.9 · Date: 17 October 2026

| Field | Value |
|---|---|
| **Status** | Draft for owner approval. Not an authorization to write code or place orders. |
| **Version / date** | 2.9 (repository edition), 17 October 2026. Supersedes v2.8 and earlier. Card word limit raised for module-sized cards; no open decision blocks M0 or M1. |
| **Owner** | Aakash (sole human supervisor) |
| **Author** | Claude, acting as Product Manager and lead architect. AI coding agents report to this role. |
| **Documentation rule** | This document follows its own rule (§9): summary first, short sections, one home per fact. |

---

## Summary

AlgoDesk Next is a small, pluggable trading workbench for Indian markets. A tiny core runs the workflow; everything that varies (brokers, data sources, execution venues, fees, indicators, screens, strategies, risk rules, notifiers, calendars, secrets, transports) is a separate plugin. One engine serves research, backtest, paper and live. Safety invariants live in code, strategies earn live status through promotion gates, every adjustable number sits in a central registry, and documents stay short and summary-first. This PRD is the requirements source for milestones M0 to M9; the Overview lists the decisions that shape everything, and later sections hold numbered requirements with acceptance tests.

## Overview (read this first)

**What it is.** AlgoDesk Next is a small, pluggable trading workbench for Indian markets. A tiny **core** runs the workflow. Everything that varies is a **plugin defined separately**: brokers, data sources, execution venues, fee schedules, indicators, screens, strategies, risk rules, notifiers, calendars, secrets and network transports.

**Eleven capabilities (your list):** broker login · fetch and store data · screens · indicators · strategy creation · backtesting · reporting and saved results · paper trading · risk management · live trading · Telegram notifications.

**Decisions that shape everything (v2.1 includes your answers of 17 Oct)**

1. **Platform, not a strategy.** No strategy, broker or fee detail lives in the core. Strategy ideas (arbitrage, ORB, SEPA) become plugins with their own one-page specs.
2. **One engine, four modes.** Research, backtest, paper and live run the *same* strategy, risk and order code. Only the clock, the data feed and the execution venue change. This removes the old AlgoDesk's two-parallel-paths problem.
3. **Complexity guidance (approved; advisory).** The old AlgoDesk grew to about 187 modules, 40,000 lines and 45 planning documents. We aim for a small core (roughly 20,000 lines) and small plugins (roughly 2,500 lines), but these are **guidance, not hard limits**: code stays as short as possible without costing quality, documentation or speed, and may grow when justified. Structural rules (one order path, no stubs, import rules) remain hard (§2.2).
4. **Central parameter registry (new).** Risk profiles, budgets, thresholds and schedules are registered in one place, changeable at any time by CLI or Telegram, every change journaled, nothing hard-coded (§4.15).
5. **Safety is built in.** Invariants live in code. Strategies move through promotion gates with stored evidence. Autonomy has levels. The kill-switch persists.
6. **Data.** yfinance, with its full capability, is the default source for research and backtests; you can name any source per run. Daily, 1-minute and live ticks are in v1, with tick-level backtests on ticks we record. Live data comes from Upstox until another broker is configured; paper and live modes require an explicit `live_suitable` source, and yfinance is accepted there only through a recorded override (§4.3).
7. **Orders.** Every order type Zerodha offers (market, limit, SL, SL-M, iceberg, GTT, cover, AMO, auction), plus **bracket as a platform feature**, because Zerodha's current official order list has no bracket order (§4.12).
8. **Markets.** NSE and BSE equity, F&O, currency and MCX, chosen per strategy or screen through instrument masters and calendar plugins.
9. **Static IP.** Zerodha rejects order calls from an IP that is not whitelisted (verified). No cloud account is needed until M7. Options are ranked zero cost first, then latency. The lead candidate is an Oracle Cloud Always Free server in Mumbai with the whole engine on that host, so there is no extra hop (§6.3).
10. **Documentation stays small.** Summary-first documents, at most 12 living documents, and **a design of 5 or more pages for every module before it is built**, condensed to a one-page README afterwards (§9).
11. **Agents.** Antigravity with Claude or Gemini switching: token-lean task cards, one `AGENTS.md` and skills keep results consistent across models. Good code from the old AlgoDesk may be reused after review (§11).

**Milestones in one line:** M0 foundations → M1 walking skeleton (first paper session) → M2 data layer → M3 indicators and screens → M4 backtester and reports → M5 risk and control → M6 Zerodha plugin and shadow mode → M7 remote runner → M8 extended paper → M9 micro-live (§12).

**Progress:** tracked in the task-card log and, from T0-9, in `docs/STATUS.md`, not in this document. Python 3.13 and TOML are decided. No open decision blocks M0 or M1.

---

## 0. How to read this document

| Label | Meaning |
|---|---|
| **Confirmed** | You decided it. |
| **Verified** | Checked against an authoritative source (Appendix E). |
| **Proposed** | My recommendation; binding only after your approval. |
| **Open** | Needs a decision or a fact. |
| **Not verified** | Believed but unchecked; do not rely on it. |

**Priorities:** P0 = milestones M0–M1 (walking skeleton). P1 = M2–M5. P2 = M6–M9 (before live). P3 = later.
Every section opens with **In short:** one or two lines you can read alone.

---

## 1. Vision, scope and lessons

**In short:** a personal, plugin-based trading workbench that earns the right to trade live, small enough for one person and AI agents to understand fully.

### 1.1 Vision
You (a retail trader acting as project manager) direct AI agents. The platform must let you go from an idea to a screened list, an indicator, a strategy, a trustworthy backtest, a paper campaign, and a carefully gated live run, without rewriting anything between stages. New brokers, strategies, indicators and fee schedules are added by dropping in a plugin, not by changing the core.

### 1.2 The eleven capabilities

| # | Capability (your list) | Section | First milestone |
|---|---|---|---|
| 1 | Broker account login and sessions | §4.1 | M1 (data), M6 (orders) |
| 2 | Fetch and store data | §4.3 | M1 basic, M2 complete |
| 3 | Screens | §4.6 | M3 |
| 4 | Indicators | §4.5 | M1 (3 indicators), M3 |
| 5 | Strategy creation | §4.7 | M1 |
| 6 | Backtesting | §4.8 | M4 |
| 7 | Reporting and saved results | §4.9 | M1 minimal, M4 |
| 8 | Paper trading | §4.10 | M1 |
| 9 | Risk management | §4.11, §8 | M1 basic, M5 |
| 10 | Live trading | §4.12 | M6–M9 |
| 11 | Telegram notifications and control | §4.13 | M1 notify, M5 control |

Supporting capabilities: instruments and calendars (§4.2), fee engine (§4.4), operations (§4.14).

### 1.3 Users and context

| Item | Detail |
|---|---|
| User | One person. Python basics, little deployment knowledge, about 95% reliant on AI for code. Explanations are written for a beginner. |
| Machines | MacBook Air M2 (8 GB) primary; Windows 10 laptop (8 GB) standby; iPhone and OnePlus for alerts. |
| Brokers at launch | Upstox (market data), Zerodha (orders). Both are plugins; others can follow. |
| Markets at launch | NSE and BSE equity, F&O, currency and MCX commodities; instruments are chosen per strategy or screen. |
| Interface | Command line plus Telegram. No web server, REST API or browser UI in v1. |
| AI tools | Antigravity (free), VS Code AI, Claude, ChatGPT Go, Gemini Pro. |

### 1.4 Goals and measures

| ID | Goal | Measure |
|---|---|---|
| G-1 | Add a new strategy without touching the core | A demo strategy plugin added in a test with zero core edits |
| G-2 | Same code for all modes | Replay test: identical decisions in backtest and paper on the same recorded data |
| G-3 | Trustworthy results | Every run reproducible from its record |
| G-4 | Stay small | Complexity budget checks pass every commit (§2.2) |
| G-5 | Understandable | Any document's summary tells the whole story in under 150 words |
| G-6 | Safe by construction | No live order path without arming and gates; kill-switch persists across restarts |
| G-7 | Near-zero cost | Cost register (§13) shows ₹0 recurring until you approve spending |

### 1.5 Non-goals (v1)
SaaS or multi-user features; REST API; browser UI (deferred to a later version, as decided earlier); machine learning; high-frequency or colocated trading; tax reporting (D-12); automatic bypass of any broker rule; promises of profit.

### 1.6 Lessons from the old AlgoDesk audit

| ID | What went wrong | Rule in this PRD |
|---|---|---|
| L-1 | Live trading bypassed the mode-invariant execution layer, leaving two parallel, inconsistent order paths | One engine for all modes (§3.3); architecture test forbids any second order path |
| L-2 | Four independent Upstox API implementations scattered around | One implementation per integration, inside its plugin (CB-3) |
| L-3 | A broker connection check that always returned success; unfinished features reported success | No stubs that return success: unsupported means `NotSupported` and a false capability flag (CB-5) |
| L-4 | An empty default holiday list despite its documentation | Fail loud on empty or missing reference data (FR-INS-4); no silent defaults |
| L-5 | 187 modules, about 40,000 lines, 45 planning documents, governance suites; contaminated source documents | Complexity budget (§2.2), documentation system (§9), scratch work kept out of docs |
| L-6 | SaaS ambitions: REST endpoints, nine phases, AI layer | CLI and Telegram only; features need a use case and a requirement ID |
| L-7 | Bugs in portfolio-mode hooks went unnoticed | Conformance suites and replay-equivalence tests per plugin kind (§10) |
| L-8 | Good assets existed (mode-invariant execution, data-provider abstraction, expression parser, indicator bridge) | Re-derive them deliberately, selectively and tested; do not copy blindly |

---

## 2. Design principles and complexity budget

**In short:** a small core, plugins for everything that varies, one path for orders, and automatic checks that keep the project small.

### 2.1 Principles

| ID | Principle |
|---|---|
| PR-1 | **Core knows contracts, not names.** The core never imports a plugin by name; it loads plugins from config through the registry. |
| PR-2 | **One engine, four modes.** Only Clock, Feed and Venue differ between modes. |
| PR-3 | **Strategies propose; the platform disposes.** A strategy returns intents. Risk and the order manager decide, and only a venue sends. |
| PR-4 | **Fail closed and loud.** Missing or invalid input blocks new exposure and raises a visible error. |
| PR-5 | **Everything that varies is data or a plugin:** fees, calendars, limits, broker quirks, risk numbers. |
| PR-6 | **Evidence before trust.** Nothing is "done" or "safe" without a test, a run record or a report. |
| PR-7 | **Reproducible by default.** Runs carry provenance (code, config, data, fee versions). |
| PR-8 | **Simple before clever.** In-process calls before queues, files before servers, SQLite before databases. |
| PR-9 | **Summary first.** Documents and reports lead with the whole story in brief. |
| PR-10 | **Deployment is outside the core.** Where and how the program runs is a profile, not a feature of the engine. |

### 2.2 Complexity guidance (Approved 17 Oct; size numbers are advisory; values live in the parameter registry; checked by `algodesk budget`)

**Size rule (17 Oct):** size numbers are guidance, not limits. Keep code minimal, but never at the cost of quality, documentation or speed; it may grow slightly when justified. `algodesk budget` reports size and warns; I review any large overshoot. Only the structural rules (CB-3 to CB-5, CB-9) and the dependency count (CB-7) fail a check.

| ID | Rule | Limit |
|---|---|---|
| CB-1 | Core size (excluding tests) | Guidance: about 20,000 lines, about 100 modules (advisory) |
| CB-2 | Plugin size | Guidance: about 2,500 lines each (advisory) |
| CB-3 | One implementation per concern | One client per broker, one order path, one calendar service, one fee engine. Enforced by import rules. |
| CB-4 | Feature admission | Needs a use case, an approved requirement ID, a test and a documentation summary line |
| CB-5 | No success-returning stubs | Unimplemented capability raises `NotSupported`; its capability flag is false |
| CB-6 | Public core API | ≤ 25 types, versioned (`api_version`) |
| CB-7 | Dependencies | Core ≤ 8 runtime packages; total ≤ 15; each justified in a decision record |
| CB-8 | Dead feature removal | A feature unused for two milestones is deleted unless you object |
| CB-9 | Architecture tests | Core imports no plugin; plugins do not import each other; strategies import no venue or broker |

### 2.3 Definition of done (every task)
Requirement ID(s) cited · code size reviewed against guidance · tests pass with output shown · documentation summary line updated · `doctor` and architecture checks pass · no unrelated changes.

---

## 3. Architecture

**In short:** a small core (config, registry, clock, data service, order manager, portfolio, risk, journal, results, scheduler, CLI) plus typed plugin kinds. Four modes share one code path.

### 3.1 Core and plugins

```
                        ┌──────────────────── CORE (small and stable) ────────────────────┐
  Config + validation   │ Plugin registry · Clock · Event dispatcher (in-process calls)    │
  Secrets provider ───► │ Data service (store + cache) · Order manager (OMS) · Portfolio   │
                        │ Risk engine · Journal/audit · Scheduler · Results store · CLI    │
                        └───────▲───────────▲───────────▲───────────▲────────────────────┘
                                │      Plugin API v1: typed interfaces + capability flags
        ┌───────────────┬───────┴──────┬────┴─────┬──────┴────┬─────┴─────┬───────────────┐
   Brokers (data,   Execution     Fee         Indicators  Screens     Strategies   Risk rules,
   auth, limits)    venues (Sim,  schedules               Calendars   Notifiers    Secrets,
   Upstox, Zerodha  Zerodha …)    (TOML data)                         (Telegram)   Transports
```

**Core responsibilities (and only these):** load and validate config; hold the central parameter registry (§4.15); discover and version-check plugins; provide the clock and event flow; store and serve data; manage orders and state; keep positions and P&L; run risk checks; journal everything; schedule jobs; save results; expose the CLI.

### 3.2 Plugin kinds

| Kind | Purpose | Examples (v1) |
|---|---|---|
| Broker / DataSource | Auth flow, instruments, history, live stream, limits | `upstox`, `yfinance` |
| ExecutionVenue | Place, modify, cancel, status, positions, funds; translates neutral orders to broker form | `sim`, `zerodha` |
| FeeSchedule | Charges as dated data | `zerodha_nse.toml` |
| Indicator | Named calculation with parameters | SMA, EMA, RSI, ATR |
| Screen | Universe plus conditions plus ranking | trend-template screen |
| Strategy | Decisions from data; returns intents | demo crossover; later arbitrage, ORB |
| RiskRule | Pre-trade check or real-time monitor | max position, daily loss |
| Notifier | Outbound messages, inbound commands | `telegram` |
| Calendar | Sessions, holidays | `nse`, `bse`, `mcx` |
| SecretsProvider | Where secrets come from | `keychain`, `env`, `aws_ssm` |
| Transport | How broker calls leave the machine (broker-specific, optional) | `direct`, `egress_proxy` |
| Fill/Slippage/Latency models | How the simulator fills orders | `conservative_book` |
| ReportTemplate | Result layouts | `standard` |

### 3.3 One engine, four modes

| Mode | Clock | Market data | Execution venue | Used for |
|---|---|---|---|---|
| Research | Real or none | Stored data | None | Screens, indicator work |
| Backtest | Simulated | Historical feed from store | `sim` (fill, slippage, fee models) | Testing ideas on history |
| Paper | Real | Live feed (data plugin) | `sim` against the live book | Proving behavior on live data |
| Live | Real | Live feed | Broker venue plugin | Real orders |
| *Shadow (P2)* | Real | Live feed | `sim`, plus a log of what live would have sent | Last step before live |

Strategy, indicators, risk engine, order manager, portfolio and journal are **identical** in all modes (PR-2).

### 3.4 Plugin rules

| ID | Rule |
|---|---|
| PL-1 | Each plugin has a folder with `plugin.toml` (name, kind, version, `api_version`, capabilities, config schema), code, tests and a `README.md` that starts with a summary (≤ 150 words). |
| PL-2 | A plugin depends only on the core API. It never imports another plugin. |
| PL-3 | Capabilities are declared. Calling an undeclared capability fails loudly. |
| PL-4 | Plugins bring their own config schema; the core validates before startup. |
| PL-5 | Every plugin kind has a **conformance suite**. A plugin is not loadable in live or paper mode until it passes (§10). |
| PL-6 | Plugin and core versions are checked at load. Incompatible versions refuse to start. |
| PL-7 | Plugin-specific facts (API limits, fees, quirks) live in that plugin's README and manifest, with source and `verified_on` date. |

### 3.5 Canonical model (core types)
`Instrument` · `Series` · `Bar` · `Tick` · `Quote` (with depth) · `OrderSpec` (broker-neutral) · `Intent` (order, cancel, modify, group) · `OrderGroup` (legs, leg policy, unhedged limit) · `Order` and `OrderEvent` · `Fill` · `Position` · `Portfolio` · `RiskDecision` · `RunRecord`.
Broker-specific features travel as declared `extensions` and are accepted only if the venue declares the capability.

### 3.6 Repository layout (new, separate repository)

```
algodesk-next/
├── README.md            # summary of everything + quick start (≤ 1 page)
├── AGENTS.md            # rules for AI coding agents (≤ 1 page)
├── docs/                # ≤ 12 living docs: INDEX, STATUS, PRD, ARCHITECTURE, PLUGINS, RUNBOOK, ...
├── src/algodesk/        # CORE ONLY
│   ├── api/             # plugin interfaces and canonical types
│   └── config/ registry/ clock/ data/ oms/ portfolio/ risk/ journal/ results/ scheduler/ cli/
├── plugins/             # everything swappable; each folder = plugin.toml + README + code + tests
│   ├── brokers/{upstox,yfinance}/   venues/{sim,zerodha}/   fees/   indicators/   screens/
│   ├── strategies/   risk_rules/   notifiers/telegram/   calendars/{nse,bse,mcx}/
│   └── secrets/{keychain,env,aws_ssm}/   transports/{direct,egress_proxy}/
├── config/              # platform.toml, profiles/*.toml (risk, deployment), runs/*.toml
├── tests/               # core, conformance, replay, architecture
├── data/ results/ logs/ # git-ignored
└── _workbench/          # git-ignored scratch: chats, prompts, drafts (never in docs)
```

### 3.7 Proposed technology (confirmed in a decision record before use)

| Area | Choice | Note |
|---|---|---|
| Language | **Python 3.13**, installed and pinned with `uv` (§3.8) | Decided 17 Oct |
| Environment | `uv` with a lock file; the same managed Python on the Mac and on any server | |
| Config format | **TOML** (Python's built-in `tomllib`, no extra package) for config, plugin manifests, parameter defaults and fee schedules | Decided 17 Oct |
| Core packages | `pydantic` (config), `numpy`, `pyarrow` | ≤ 8 allowed (CB-7) |
| Research extras | `pandas`, plotting library | Not used in the live path |
| CLI | `typer` | Small; `rich` optional |
| Storage | Parquet for series; SQLite for manifest, journal, results index | Zero cost, one file each |
| Scheduler | Small built-in asyncio scheduler | APScheduler only if justified |
| Concurrency | `asyncio`, single event loop | In-process, no external queue |
| Plugins bring | `websockets`, `protobuf`, broker SDKs, Telegram HTTP client | Counted against CB-7 |
| Quality | `pytest`, `ruff`, `mypy`, `import-linter` | Architecture tests (CB-9) |

### 3.8 Python version decision (17 Oct)

**Chosen: Python 3.13**, installed and pinned by `uv`: `uv python install 3.13`, a `.python-version` file, `requires-python = ">=3.13,<3.14"` and the `uv.lock` file. Antigravity then uses the project's `.venv`, not any of the five interpreters on your Mac.

| Option on your Mac | Verdict | Why |
|---|---|---|
| **3.13 (managed by uv)** | **Use** | Mature, widely supported by libraries, security fixes until about October 2029 (general knowledge, not checked today). The Zerodha client's current release was itself built with CPython 3.13.7 (PyPI). |
| 3.14.4 (Homebrew default) | Not yet | Newest. Early in its life, library floors had to be raised to get wheels (for example pydantic 2.12 or newer, from a project's own upgrade notes). The old broker SDKs are the likely friction: the Upstox SDK still declares "Python 2.7 and 3.4+" and the Kite client pulls the autobahn and Twisted stack. Its speed gain does not matter for an input/output-bound design. Revisit at M7 with a non-blocking test run. |
| 3.13.7 (Homebrew) or 3.13.2 (`/usr/local`) | Do not use directly | Same version family, but a Homebrew upgrade can change it under the project, and the `/usr/local` copy is an older patch outside Homebrew and uv. uv installs its own 3.13 and keeps it stable. |
| 3.11.15 | No | Works with everything, but security support ends about October 2027 (general knowledge), which forces an early migration. |
| 3.9.6 (macOS system) | No | End of life; never use the system Python for projects. |

**Cloud server:** `uv python install 3.13` installs the identical interpreter on any server, so the server's own Python is irrelevant (Ubuntu 24.04 ships 3.12 and Oracle Linux 9 ships 3.9). Both macOS arm64 and Linux aarch64 (Oracle Arm) are standard wheel targets; I did not check every package individually, so `uv sync` on both machines at M7 confirms it (**Not verified**).

---

## 4. Capability requirements

Each row: ID · requirement · priority · acceptance test. Every section starts with its one-line summary.

### 4.1 Broker connectivity and sessions (FR-BRK)
**In short:** brokers are plugins that declare what they can do, how to log in, and their limits. The core handles sessions without knowing any broker.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-BRK-1 | A broker plugin declares identity, capabilities (history, stream, orders, positions, funds, margins), supported segments, products and order types, rate and connection limits, auth flow, and any required transport (for example static IP). | P0 | Manifest validates; undeclared calls fail loudly |
| FR-BRK-2 | Auth flows are pluggable: interactive (user opens a link, completes two-factor login, returns a code), token, or API key. Tokens are stored through the SecretsProvider with their expiry. | P0 | Interactive flow tested with a fake broker |
| FR-BRK-3 | A session manager checks validity at startup and on schedule, warns before expiry and guides the user through login by CLI or Telegram. It never loops on failed logins. | P1 | Expired-token test |
| FR-BRK-4 | A run chooses `data_source` and `execution_venue` independently (for example Upstox data with Zerodha orders). | P0 | Run config test |
| FR-BRK-5 | Cross-broker mapping uses canonical instrument ids (FR-INS-1). | P0 | Mapping tests |
| FR-BRK-6 | Automating two-factor logins is out of scope unless the broker's terms explicitly allow it. | P1 | Documented in each broker plugin |
| FR-BRK-7 | Limits declared by a plugin are enforced by one core rate limiter. | P1 | Limiter test |
| FR-BRK-8 | Broker health (round-trip time, auth validity, error rate) feeds the risk engine as signals. | P1 | Probe test |

### 4.2 Instruments, markets and calendars (FR-INS)
**In short:** one canonical instrument identity, translated per broker; calendars fail loudly when empty.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-INS-1 | Canonical ids (for example `NSE:EQ:RELIANCE`, `NSE:FUT:RELIANCE:2026-10-27`). Each broker plugin maps both ways. | P0 | Round-trip test per broker |
| FR-INS-2 | Instrument masters are downloaded, versioned and diffed daily (lot sizes, expiries, tick sizes). | P1 | Diff report |
| FR-INS-3 | Derivative contract resolution (near and next expiry, rollover) is generic and strategy-independent. | P1 | Fixtures around expiry dates |
| FR-INS-4 | The Calendar plugin provides sessions, holidays and special sessions. Empty or insufficient coverage stops the run with a clear error. | P0 | Empty-calendar test |
| FR-INS-5 | Universes (lists, indices, screen outputs) are named, versioned and reusable. | P1 | Test |
| FR-INS-6 | Markets are data: v1 covers NSE and BSE equity, F&O, currency and MCX through broker instrument masters and calendar plugins (`nse`, `bse`, `mcx`). Universes are chosen per strategy or screen. | P1 | Adding a dummy market needs no core change |

### 4.3 Data: fetch, store, cache (FR-DAT)
**In short:** callers ask for data; the data service serves it from cache and fetches only what is missing, with validation. Your requirement D-07 is kept.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-DAT-1 | One `DataService.get(series, range)`. Callers never fetch directly. Existing data is never re-downloaded. | P0 | Second identical request makes zero network calls |
| FR-DAT-2 | Series identity: instrument, kind (bars, ticks, depth), timeframe, adjustment, source. | P0 | Schema test |
| FR-DAT-3 | Parquet files partitioned by series and period; SQLite manifest of coverage, row counts, checksums, source version and validation status; atomic writes. | P1 | Crash-during-write test |
| FR-DAT-4 | Incremental update and gap-fill. Staleness is judged against the calendar (last completed session). | P1 | N → N+1 day test fetches one session |
| FR-DAT-5 | Validation on every write: columns, types, monotonic timestamps, no duplicates, OHLC sanity, non-negative volume, session hours. Failures go to quarantine; good data is never overwritten silently; gaps are reported, never invented. | P1 | Corrupt-fixture tests |
| FR-DAT-6 | Live ticks and bars are recorded into the same store, so paper and live sessions become backtestable history. | P1 (basic in M1) | Replay reproduces the session |
| FR-DAT-7 | Each run records the manifest ids and checksums of the data it used. | P1 | Run record check |
| FR-DAT-8 | Bars are built from ticks by one core aggregator, identical in live and backtest. | P1 | Equivalence test |
| FR-DAT-9 | Corporate actions: raw prices stored, adjustment factors kept separately, adjusted views derived. Raw and adjusted are never mixed silently. | P2 | Policy test |
| FR-DAT-10 | Several sources per series with priority; every row tagged with its source. | P2 | Test |
| FR-DAT-11 | `data status` and `data verify` commands. | P1 | CLI tests |
| FR-DAT-12 | Retention policy (compress or prune old ticks) is configurable. | P2 | Test |
| FR-DAT-13 | A run names its data source. If none is named, the default is `yfinance`. | P1 | Config test |
| FR-DAT-14 | Every DataSource declares `history`, `stream`, `realtime`, `delay`, `intervals`, `retention`, `official_api` and `live_suitable`. Paper and live modes require an explicit `live_suitable` source (today: Upstox) and refuse any other, except that yfinance is accepted in paper through a recorded override (decided 17 Oct). | P1 | Test: yfinance refused in live and refused in paper without the override |
| FR-DAT-15 | The `yfinance` plugin exposes its full capability: bars at every interval Yahoo offers (raw and split- and dividend-adjusted OHLCV, valued for backtests), splits and dividends, fundamentals, company info, options chains and delayed quotes (the Yahoo stream is optional and marked experimental). It maps NSE (`.NS`) and BSE (`.BO`) symbols, limits itself politely (default at most 2 requests per second, back-off on HTTP 429) and declares each limit in its manifest. | P1 | Conformance suite and live smoke test |
| FR-DAT-16 | A scheduled **accumulation job** copies short-retention intraday series (for example yfinance 1-minute bars, about 7 days) into the store every day, so history builds over time. | P1 | Job test with a fake source |
| FR-DAT-17 | Tick-level backtests use ticks we recorded (FR-DAT-6) or vendor files imported through a DataSource plugin. The Upstox and yfinance interfaces checked offer no historical ticks. | P1 | Import test |

### 4.4 Fee and charges engine (FR-FEE)
**In short:** charges are dated data files read by one generic calculator, so backtests use the fees that applied on each trade date.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-FEE-1 | A schedule is a TOML file per broker, segment and product: components as a rate on turnover, flat per order, caps and minimums, taxes on components (GST), by side. | P0 | Schema test |
| FR-FEE-2 | Schedules are effective-dated. A backtest selects the schedule valid on each trade date. | P1 | Date-boundary test |
| FR-FEE-3 | Each schedule records source and `verified_on`; a warning appears when older than 90 days. | P1 | Test |
| FR-FEE-4 | A table of test vectors compares the engine with the broker's own calculator, tolerance ₹1 per trade. | P0 | Test table |
| FR-FEE-5 | Each simulated or real fill stores its charge breakdown. | P0 | Journal check |

### 4.5 Indicators (FR-IND)
**In short:** an indicator is defined once and computed two ways (batch and incremental) that must agree.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-IND-1 | A registry; each indicator declares inputs, parameter schema, outputs and warm-up length. | P0 | Registry test |
| FR-IND-2 | Batch and incremental evaluation share one definition and must match on golden data. | P1 (3 indicators in M1) | Equivalence test |
| FR-IND-3 | Built-in set: SMA, EMA, RSI first (P0); ATR, VWAP, Bollinger, MACD, rolling high/low, volume average, rate of change (P1). Validated against published formulas or reference vectors. | P0/P1 | Golden-vector tests |
| FR-IND-4 | Custom indicators are Python files using the same contract plus a required golden test. A scaffold command creates them. | P1 | Scaffold test |
| FR-IND-5 | No look-ahead: indicators see completed bars only; partial-bar use must be explicit. | P0 | Look-ahead test |
| FR-IND-6 | Multi-timeframe use (daily inside intraday) through core resampling, without look-ahead. | P2 | Test |
| FR-IND-7 | Declarative composite formulas (for example `EMA(close,20) - EMA(close,50)`) via a restricted, safe expression parser (no `eval`). | P2 | Injection tests |
| FR-IND-8 | Optional library adapters (for example TA-Lib) as plugins; built-ins have no external dependency. | P3 | Adapter test |

### 4.6 Screens (FR-SCR)
**In short:** a screen filters and ranks a universe using data and indicators, saves the result with provenance, and can feed a strategy.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-SCR-1 | A screen = universe + conditions over indicators and data + optional ranking + output. | P1 | Example screen |
| FR-SCR-2 | Simple screens are TOML rules; complex ones are Python plugins. Both register identically. | P1 | Both forms run |
| FR-SCR-3 | Runs on stored data, on demand or scheduled (for example after close). The output (table plus summary) is saved as a run. | P1 | Result folder exists |
| FR-SCR-4 | A strategy can use a screen's output as its universe (`universe: screen:<name>`). | P1 | Integration test |
| FR-SCR-5 | A 500-symbol daily-bar screen completes in ≤ 60 s on the MacBook (Proposed). | P1 | Benchmark |
| FR-SCR-6 | Point-in-time (as-of date) screening for honest historical tests. | P2 | Test |
| FR-SCR-7 | Screen results can be sent as a Telegram digest. | P2 | Test |

### 4.7 Strategies (FR-STR)
**In short:** a strategy is a plugin with parameters and hooks. It returns intents and never touches a broker.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-STR-1 | Contract: parameter schema; hooks `on_start`, `on_bar`, `on_tick`, `on_fill`, `on_timer`, `on_stop`; returns intents. | P0 | Demo strategy runs |
| FR-STR-2 | Intents are broker-neutral: order, cancel, modify, and **order group** (multiple legs, leg policy, unhedged-time limit) so spreads, pairs and arbitrage fit without core changes. | P0 (group P1) | Group test |
| FR-STR-3 | The context is read-only: clock, data, indicators, positions, risk state, parameters. | P0 | Mutation attempt fails |
| FR-STR-4 | Parameters live in TOML, validated by schema, and recorded in every run. | P0 | Test |
| FR-STR-5 | State persistence: a strategy declares serializable state, the platform checkpoints it, and a restart resumes correctly. | P1 | Kill-and-restart test |
| FR-STR-6 | A scaffold (`algodesk new plugin strategy <name>`) plus a **strategy conformance suite**: deterministic, no look-ahead, no broker imports, state round-trip, restart-safe. | P1 | Suite passes for the demo |
| FR-STR-7 | Reusable helpers for risk-first sizing (stop → rupee risk → quantity) and stops/targets. | P1 | Unit tests |
| FR-STR-8 | Declarative rule strategies (TOML entry/exit rules, sizing, stops). Deferred: v1 strategies are Python (decided 17 Oct). | P3 | Not in v1 |
| FR-STR-9 | Several strategies run together under shared account-level risk. | P2 | Test |
| FR-STR-10 | The lifecycle state of each strategy is stored and enforced (§5). | P1 | Promotion test |

### 4.8 Backtesting (FR-BKT)
**In short:** backtest is a mode of the same engine, with conservative cost and fill models and loud data checks.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-BKT-1 | Backtest mode = simulated clock + historical feed + `sim` venue. No separate backtest engine. | P1 | Architecture test |
| FR-BKT-2 | A data-readiness check runs first: coverage, gaps, staleness. It fails with exactly what is missing and offers to fetch it. | P1 | Test |
| FR-BKT-3 | Selectable fill, slippage, latency and fee models; conservative defaults; choices recorded. | P1 | Test |
| FR-BKT-4 | No look-ahead by construction: an order decided on bar *t* cannot fill before bar *t+1*, unless a tick-level model is chosen. | P1 | Look-ahead test |
| FR-BKT-5 | Portfolio mode: many instruments, shared capital, position limits. | P1 | Test |
| FR-BKT-6 | Deterministic: same inputs and seed give identical results (result hash). | P1 | Repeat-run test |
| FR-BKT-7 | `sim` supports every order type in the neutral `OrderSpec` (§4.12): market (with protection), limit, stop and stop-market first (P1); then iceberg, cover, GTT, bracket, AMO and auction (P2), each with documented semantics. A type a venue does not declare is rejected loudly. | P1/P2 | Per-type tests |
| FR-BKT-8 | Parameter sweeps, out-of-sample split and walk-forward utilities. Reports show the number of trials. | P2 | Test |
| FR-BKT-9 | Tick-level backtest and replay of recorded or imported ticks for latency-sensitive strategies. | P1 | Tick-replay test |
| FR-BKT-10 | Speed target (Proposed): 100 symbols × 1 year of daily bars ≤ 30 s; of 1-minute bars ≤ 10 min on the MacBook. | P1 | Benchmark |

### 4.9 Reporting and results store (FR-RPT)
**In short:** every run saves an immutable result folder that starts with a one-page summary and shares one schema across backtest, paper and live.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-RPT-1 | Each run (backtest, paper session, live day, screen) writes `results/<run_id>/` with `SUMMARY.md` (≤ 150 words: verdict, key numbers, caveats), `metrics.json`, trades, equity, config snapshot, manifest. | P1 (minimal in M1) | Folder check |
| FR-RPT-2 | A results index in SQLite and commands to list, show, compare and delete runs. | P1 | CLI test |
| FR-RPT-3 | Standard metrics with documented definitions: return, CAGR, volatility, Sharpe, Sortino, maximum drawdown and duration, win rate, expectancy, profit factor, exposure, turnover, total charges, trade count. | P1 | Metric tests with known answers |
| FR-RPT-4 | Every report lists its assumptions and limitations: fill model, fee version, data range, trial count. | P1 | Template check |
| FR-RPT-5 | Offline HTML report (equity curve, drawdown, trade distribution), no external resources. | P2 | Test |
| FR-RPT-6 | Backtest, paper and live runs compare through one schema, including drift (expected vs actual). | P2 | Test |
| FR-RPT-7 | Retention and size caps for results. | P2 | Test |

### 4.10 Paper trading (FR-PAP)
**In short:** live data, simulated orders, the same code as live, and fills that are never flattering.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-PAP-1 | Paper mode = live feed + `sim` venue; identical strategy, risk and order code. | P0 | Replay-equivalence test |
| FR-PAP-2 | Conservative fills: walk visible book depth, apply a latency model, allow partial fills and rejections, and never fill at prices that were not in the book. | P0 | Fixture tests |
| FR-PAP-3 | A virtual account (capital, margin) follows the product rules declared by the venue plugin. | P0 | Test |
| FR-PAP-4 | Session reports, plus campaign reports across many days. | P1 | Test |
| FR-PAP-5 | The paper process holds no order-capable credentials, so it cannot reach real order endpoints. | P0 | Static and runtime test |
| FR-PAP-6 | Failure injection (rejects, timeouts, disconnects) for testing recovery. | P1 | Tests |

### 4.11 Risk management (FR-RSK)
**In short:** layered checks, with invariants in code and numbers in config. Detail in §8.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-RSK-1 | Every intent passes pre-trade rules from the selected risk profile. Each rejection records the rule and values. | P0 | Table-driven tests |
| FR-RSK-2 | Rules and monitors are `RiskRule` plugins; profiles are registered in the central parameter registry (§4.15) as percentages of allocated capital and can change at any time under the rules there; the most restrictive of global and per-strategy limits wins. | P1 | Test |
| FR-RSK-3 | Real-time monitors: daily and weekly loss, drawdown, exposure, margin buffer, order rate, unhedged group timers. | P1 | Tests |
| FR-RSK-4 | Health guards: stale data, clock skew, broker error rate, reconciliation mismatch. | P1 | Tests |
| FR-RSK-5 | Kill-switch (manual or automatic) blocks all new exposure, survives restarts and clears only by explicit command. | P0 | Restart test |
| FR-RSK-6 | A strategy cannot disable or bypass risk; the order manager is the only path to a venue. | P0 | Architecture test |
| FR-RSK-7 | A per-strategy outage policy (§8.4). | P1 | Test |

### 4.12 Live trading and order management (FR-LIV)
**In short:** one order manager and state machine for all modes; live adds only a broker venue plugin, arming, reconciliation and autonomy levels.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-LIV-1 | Order state machine: created → submitted → acknowledged → partially filled → filled; cancel requested → cancelled; rejected; unknown → reconciling. Every transition is journaled. Used in all modes. | P0 | State tests |
| FR-LIV-2 | Idempotent client references; a timeout never triggers a blind retry; unknown outcomes are reconciled first. | P0 | Injection tests |
| FR-LIV-3 | Execution venue plugins implement one contract; venue-specific behavior (product mapping, market protection, quantity freeze splitting) stays inside the plugin. | P2 | Conformance suite |
| FR-LIV-4 | Startup and periodic reconciliation of orders, positions and funds. A mismatch halts new orders and alerts. | P2 | Mismatch test |
| FR-LIV-5 | **Arming:** live needs, per strategy, a config flag, an environment variable, a gate file with passed gates and a typed confirmation. | P2 | Test |
| FR-LIV-6 | **Autonomy levels:** L0 signals only; L1 approve each order by Telegram; L2 automatic within limits. | P2 | Tests |
| FR-LIV-7 | A live budget per strategy, starting small, raised only by an explicit recorded config change. | P2 | Test |
| FR-LIV-8 | Shadow mode: live feed, would-be orders computed and logged, nothing sent. | P2 | Test |
| FR-LIV-9 | Protective orders at the venue where supported (resting stops), so protection survives a platform outage. | P2 | Venue test |
| FR-LIV-10 | End-of-day handling for intraday products (square-off time, broker auto square-off awareness). | P2 | Test |
| FR-LIV-11 | Emergency commands: pause strategy, kill-switch, and flatten where the strategy's policy allows. | P2 | Tests |
| FR-LIV-12 | A neutral `OrderSpec` covers market, limit, stop (SL), stop-market (SL-M), iceberg, GTT (single and two-leg), bracket, cover, AMO and auction. Each venue declares which are **native**, **emulated** or **unsupported** in a matrix. | P2 | Matrix test per venue |
| FR-LIV-13 | **Bracket orders are a platform feature:** entry plus stop plus target managed by the order manager as one linked group (one cancels the other), using resting venue-side stops where available. Zerodha's current official variety list has no bracket order (N-14). | P2 | Group-lifecycle tests |
| FR-LIV-14 | Venue rules live in the venue plugin: non-zero market protection for market and SL-M orders; no IOC on MCX algo orders; iceberg 2–10 legs and slicing capped at 10; order rate below the broker limit. | P2 | Venue tests |
| FR-LIV-15 | Order types have identical semantics in `sim` and live, so a strategy tested in backtest behaves the same live. | P2 | Replay-equivalence per type |

### 4.13 Notifications and remote control (FR-NTF)
**In short:** Telegram tells you what matters, accepts a few safe commands from you only, and never blocks trading.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-NTF-1 | A Notifier plugin contract: severities (info, warning, critical), rate limiting, daily digest. Delivery failure never blocks trading. | P0 | Test |
| FR-NTF-2 | Telegram events: start and stop, feed and broker health, signals and orders (rate-limited), fills, risk breaches, kill-switch, errors, daily summary. | P0 | Test bot |
| FR-NTF-3 | Commands from your chat id only: `/status`, `/pause`, `/resume`, `/killswitch`, `/positions`, `/report`, and `/approve` or `/reject` at autonomy L1. Other senders are ignored and logged. | P1 | Tests |
| FR-NTF-4 | Telegram uses outbound long-polling, so no inbound port is needed on any machine. | P1 | Design check |
| FR-NTF-5 | Messages never contain secrets or full account identifiers. | P0 | Test |
| FR-NTF-6 | Heartbeat messages; silence is itself a warning (dead-man check). | P1 | Test |

### 4.14 Operations (FR-OPS)
**In short:** one `doctor` check, a daily routine defined in config, restart-safe state and simple backups.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-OPS-1 | `algodesk doctor`: config valid, plugins compatible, sessions valid, calendar covers today, instrument master fresh, disk space, clock offset, notifier reachable, journal closed cleanly, environment isolated (NFR-14). | P0 | Output check |
| FR-OPS-2 | A daily routine defined in config (login ceremony, doctor, start, end-of-day jobs, data refresh, backup). | P1 | Schedule test |
| FR-OPS-3 | Graceful stop. After a crash, state is rebuilt from the journal and checkpoints, then reconciled. | P1 | Kill test |
| FR-OPS-4 | Headless operation: the same CLI runs on any machine from files and secrets, with a supervisor restarting it (launchd on macOS, systemd on Linux). | P1 | Smoke test |
| FR-OPS-5 | Journal and config backups to a second location. | P1 | Test |
| FR-OPS-6 | Structured logs with a run id, rotation and retention. | P0 | Check |
| FR-OPS-7 | `algodesk budget` reports size against the (advisory) guidance and fails only on dependency count and structural rules; `algodesk docs check` enforces the documentation rules. | P0 | Commands |

### 4.15 Central parameter registry (FR-REG)
**In short:** every adjustable number (risk limits, budgets, thresholds, schedules, fee-schedule choice) is registered in one place, changeable at any time with an audit trail, and never hard-coded.

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| FR-REG-1 | One registry holds named, typed, versioned parameters. Defaults ship as TOML files (risk profiles, complexity budget, staleness limits, order limits, schedules). Plugins register their own parameters with schema, default and bounds. | P0 | Registry test |
| FR-REG-2 | Code contains no limit constants. A lint check fails on numeric literals used as limits outside the registry and tests. | P0 | Lint test |
| FR-REG-3 | Parameters change at any time with `algodesk param set` or Telegram `/param`, validated against bounds. Who, when, old value and new value are journaled. | P1 | Change test |
| FR-REG-4 | Hot reload at safe points (between events). Tightening a limit applies immediately. Loosening a limit while a live strategy is armed needs typed confirmation and takes effect at the next session start (Approved 17 Oct). | P1 | Test |
| FR-REG-5 | Every run records the registry snapshot (hash and values) in its run record. | P0 | Run record check |
| FR-REG-6 | `algodesk param show` lists current values, defaults and last change. | P1 | CLI test |

---

## 5. Strategy lifecycle, promotion gates and autonomy

**In short:** strategies move Draft → Backtested → Paper → Live-candidate → Live, and the platform checks stored evidence at each step. Autonomy rises separately.

| State | Meaning | Entry evidence (stored as run ids in the journal) |
|---|---|---|
| Draft | Being built | Strategy conformance suite passes |
| Backtested | Backtest meets criteria | Saved backtest runs with data readiness passed, realistic costs, an out-of-sample period untouched by tuning, and trial count reported |
| Paper | In a paper campaign | A risk profile assigned and a campaign plan written **before** it starts |
| Live-candidate | Paper campaign passed | Campaign report meets the plan's criteria; your review recorded |
| Live (L0 / L1 / L2) | Armed | All live gates (§8.5) and your explicit approval |
| Retired | Stopped | Reason recorded |

- Criteria are **per strategy** (a swing strategy has few trades; an intraday one has many). They are written down before each stage starts. The platform defaults (Proposed): at least 100 backtest trades or a written reason; paper at least 20 trading days or 30 trades, whichever is longer.
- Promotion is a CLI command that verifies the evidence exists. It cannot be forced without a recorded override and a Telegram notice.
- Demotion is automatic: a risk breach or failed reconciliation returns the strategy to Paper pending your review.

---

## 6. Deployment and egress (not part of the core)

**In short:** where the program runs is a *profile*. Research, backtests and paper trading run on the MacBook. Zerodha rejects order calls from an IP that is not whitelisted, so live trading runs on a cloud server that has a static IP, with the whole engine on that host so there is no extra hop between engine and broker. The core never mentions any cloud; the Zerodha plugin only declares "orders need a whitelisted static IP". Your priorities: zero cost first, then lower latency.

### 6.1 Deployment profiles

| Profile | Where the engine runs | Order path | Extra hop vs. a home setup | Use |
|---|---|---|---|---|
| **A. Local** | MacBook | Home internet | None | Research, backtest, paper. Live orders are blocked unless a static IP exists at home. |
| **B. Remote runner (Proposed for live)** | A cloud server with a static IP (any provider); the MacBook is control and research | Server → broker | **None.** Feed and orders both leave the same host; home internet only affects your control and alerts. | Live trading |
| **C. Local engine + egress proxy** | MacBook | MacBook → proxy on a static-IP host → broker | **One hop** on order calls only | Slower strategies, if B is not wanted |

- **Recommendation:** B for live. It satisfies the static IP and adds no hop between engine and broker. C is a fallback; its added delay is measured before use.
- The profile is one config file. The same `algodesk` CLI runs on both machines. The core has no cloud code.
- Broker-specific transport (direct or proxy) is a `Transport` plugin used by the broker plugin; the core does not know it exists.

### 6.2 Requirements for the remote runner (broker- and cloud-independent)

| ID | Requirement | Pri | Acceptance |
|---|---|---|---|
| DEP-1 | Deployment profile files select the machine role, secrets provider and transport. No cloud-specific code in the core. | P2 | Architecture test |
| DEP-2 | Headless operation under a supervisor (systemd on Linux) with automatic restart and a crash-loop limit. | P2 | Kill test |
| DEP-3 | Secrets through a SecretsProvider: `keychain` on the Mac; a permission-restricted file or the cloud's own secret store on the server. Order-capable credentials exist **only** on the live runner. | P2 | Test |
| DEP-4 | No inbound ports except restricted administration. Telegram is outbound only. | P2 | Port check |
| DEP-5 | Clock synchronization check at startup (NTP). | P2 | Doctor check |
| DEP-6 | **Login ceremony:** each morning the runner sends a login link to Telegram. You log in on your phone and send the returned code back. No stored passwords and no automated two-factor. | P2 | Test with a fake broker |
| DEP-7 | Journal and results are copied back to the MacBook (for example `rsync` or `scp`, no paid service) and summarized by a daily digest. | P2 | Test |
| DEP-8 | A probe tool (planned for M7) measures feed delay, broker API round-trip time and a dry-run order round trip, and prints per-profile latency. Decisions on B vs C use measured numbers. | P1 | Output |
| DEP-9 | A cost guard: a billing alert at a low threshold on any paid cloud account (your action; documented in the runner guide). | P2 | Checklist |
| DEP-10 | Live failover is manual: positions are visible and manageable in the broker app on your phone; the runner restarts under its supervisor. Paper and research can fail over to the Windows laptop. | P2 | Runbook |

Provider-specific setup steps live in a separate runner guide (`docs/RUNNER.md`, ≤ 2 pages), not here.

### 6.3 Static IP: verified rules and options

**Zerodha rules (Verified; Zerodha team posts in the Kite developer forum, thread "Preparing to comply with SEBI's retail algo rules"):**
- Order calls from an IP that is not on your developer profile's whitelist are rejected. This applies to every API order regardless of frequency. Only order endpoints are checked: market data, WebSocket streams, order book and positions work from any IP.
- The IP can come from an ISP, a cloud provider or a VPN/VPC service. IPv4 or IPv6 is accepted, but the whitelisted address must match the family the request actually leaves from.
- One IP can be linked to only one developer account. Family sharing (spouse, dependent children, dependent parents) is done inside one developer profile.
- Related algo rules in the same post: a strict 10 orders per second per client id (HTTP 429 above), non-zero market protection for market and SL-M orders, no IOC on MCX, and order slicing capped at 10.

**Options (zero cost first, then latency):**

| # | Option | Cost | Latency and path | Notes |
|---|---|---|---|---|
| 1 | **Oracle Cloud Always Free VM with a reserved public IPv4** (lead candidate) | ₹0 within Always Free limits | Same city as the exchanges if your home region is Mumbai; measure against the broker endpoints (N-10) | Needs card verification at signup. Micro VM: 1 OCPU, 1 GB RAM, up to 50 Mbps. Arm VMs up to 4 OCPU and 24 GB if capacity exists. The IP must be changed from ephemeral to *reserved*. Home region is fixed at signup. Idle-instance policy to check (N-12). |
| 2 | AWS new account | Credits (about $100–200) valid up to 12 months, then paid; public IPv4 about $0.005 per hour | Mumbai region | Fallback. Credits are not free forever (VF-11, VF-12). |
| 3 | India VPS, cheap proxy or dedicated-IP VPN | Not ₹0 (forum posts mention a few dollars a month; **Not verified**) | Extra hop if used as a proxy | Reliability varies. |
| 4 | Home ISP static IP | Usually paid (N-11) | No cloud hop if the engine runs at home | Needs dependable home power and internet. |
| 5 | Home IPv6 | May cost nothing; Zerodha accepts IPv6 whitelisting | Same as home | ISP prefixes often change and requests must really leave over IPv6 (**Not verified**). |

**Recommendation (Proposed):** do nothing now, because the static IP is first needed at M7 (and live at M9). At M7: create an Oracle Always Free account with a Mumbai home region, reserve the IP, run the planned probe tool and compare with option 2 if Oracle capacity or signup fails. Run the whole engine on that host for live (Profile B). Decision recorded then.

### 6.4 Where latency comes from and what we control

| Stage | What we control |
|---|---|
| Exchange to broker feed | Nothing |
| Feed to our process | Run the engine in a Mumbai-region host; one persistent WebSocket per feed |
| Decode and normalize | Use the broker's Protobuf decoder; avoid creating objects per tick |
| Strategy and indicators | Incremental indicators (constant work per tick); no input/output inside hooks |
| Risk checks | Limits pre-read from the registry snapshot; constant-time checks |
| Order build and send | Pre-built order templates; a persistent pooled HTTPS connection; no proxy hop (Profile B) |
| Broker order system and exchange | Nothing |

Measure, do not guess: the probe tool reports each stage, and the platform's own overhead budget is 5 ms at the 95th percentile (NFR-1). A retail setup cannot match colocated firms; the realistic goal is low and *predictable* latency.

---

## 7. Non-functional requirements

**In short:** the platform adds almost no latency, fits a 1 GiB server, stays reproducible, and keeps secrets out of everything.

| ID | Area | Requirement | Pri |
|---|---|---|---|
| NFR-1 | Hot-path overhead | Platform-added delay from tick received to order submitted (excluding strategy compute and network): p95 ≤ 5 ms (Proposed). Hot path uses direct in-process calls; no serialization or external queue. | P1 |
| NFR-2 | End-to-end latency | Reported per deployment profile from measured numbers (DEP-8), never assumed. | P1 |
| NFR-3 | Memory | Live runner ≤ 512 MB resident with 200 instruments; research on the MacBook ≤ 4 GB. | P1 |
| NFR-4 | Determinism | Same inputs and seed give identical decisions (replay tests). | P1 |
| NFR-5 | Reliability | A full session without intervention; automatic reconnects; restart recovery in under 60 s. | P1 |
| NFR-6 | Time | UTC in storage, IST in display, clock offset checked at startup. | P0 |
| NFR-7 | Security | Secrets never in code, config, logs or chat; least privilege (order-capable credentials only where live runs). | P0 |
| NFR-8 | Portability | macOS, Linux server, Windows 10 smoke test. | P1 |
| NFR-9 | Observability | Structured logs with a run id; rotation and retention. | P0 |
| NFR-10 | Maintainability | Typed, documented, within the complexity budget. | P0 |
| NFR-11 | Extensibility | A new plugin needs no core change; plugin API changes follow versioning rules with migration notes. | P1 |
| NFR-12 | Data safety | Atomic writes; daily backups of journal and manifest. | P1 |
| NFR-13 | Cost | ₹0 recurring until you approve spending. | P0 |
| NFR-14 | Local isolation | Everything for the project stays inside the project folder: Python, libraries, caches, tool folders and hook environments live under `.venv/` and `.local/`; all commands run through `uv run` after `source scripts/env.sh`; no global `pip`, `brew`, `npm -g` or `uv tool` installs; no user-level packages are loaded; testing happens only in the virtual environment. Programs you already had (`uv`, `git`) stay global. `doctor` and `scripts/check.sh` verify it. | P0 |

---

## 8. Safety and risk policy

**In short:** invariants cannot be configured away; limits are percentages in profiles you approve; outages follow a per-strategy policy; live needs ten gates and your approval.

### 8.1 Safety invariants (in code; not changeable by config)

| ID | Invariant |
|---|---|
| INV-1 | Research, backtest and paper processes cannot load an order-capable venue plugin through the registry; its unit tests import it only with fakes and no credentials, and order-capable credentials exist only on the live runner. |
| INV-2 | Live orders need arming (FR-LIV-5) per strategy. |
| INV-3 | Orders reach a venue only through the order manager after risk approval. |
| INV-4 | The kill-switch persists across restarts. |
| INV-5 | Unknown, stale or invalid inputs block new exposure (fail closed). |
| INV-6 | Every order has a unique client reference; timeouts reconcile before any retry. |
| INV-7 | Strategies cannot disable risk. Limit changes go through config and are journaled. |
| INV-8 | Every decision, rejection and order event is journaled. |
| INV-9 | Secrets are never written to logs, reports or alerts. |
| INV-10 | Unsupported capability raises an error. |
| INV-11 | A live process refuses to start when critical `doctor` checks fail. |

### 8.2 Risk layers

| Layer | What | Examples |
|---|---|---|
| R1 Pre-trade rules | Check each intent | Position size, exposure, price deviation, order value, allowed window |
| R2 Real-time monitors | Watch the portfolio | Daily and weekly loss, drawdown, margin buffer, order rate, unhedged group timers |
| R3 Health guards | Watch the system | Stale data, clock skew, broker errors, reconciliation mismatch |
| R4 Kill-switch | Stop new exposure | Manual, automatic on breach, persistent |
| R5 Venue protections | Broker-side safety | Market-protection parameter, price bands, resting stop orders |

### 8.3 Default risk profile "conservative" (Approved 17 Oct; held in the parameter registry, changeable any time; percentages of allocated capital)

| Parameter | Default | Meaning |
|---|---|---|
| `per_trade_risk_pct` | 0.5 | Maximum loss if a trade's stop is hit |
| `max_position_pct` | 20 | Largest single position |
| `max_open_positions` | 5 | Concurrent positions or groups |
| `max_gross_exposure_pct` | 100 | Total exposure (unleveraged default) |
| `margin_buffer_pct` | 25 | Free cash kept for margin demands |
| `daily_loss_limit_pct` | 1.5 | Stop new entries for the day |
| `weekly_loss_limit_pct` | 3.0 | Stop new entries for the week |
| `drawdown_halt_pct` | 6.0 | Halt until reviewed |
| `max_order_value_pct` | 25 | Fat-finger guard |
| `price_deviation_pct` | 2.0 | Reject orders far from the last price |
| `max_orders_per_minute` | 20 | Self-imposed, below broker limits |
| `max_daily_trades` | 20 | Overtrading guard |
| `stale_data_seconds` | 5 | Per feed; strategies may tighten it |
| `max_unhedged_seconds` | 5 | Order groups with several legs |

Profiles are cheap to add (for example a swing profile with 1% risk per trade). A percentage rule does not guarantee safety: gaps, slippage, rejections and margin calls can exceed it, and reports say so.

### 8.4 Outage policy (per strategy)

| Situation | System action | Human action |
|---|---|---|
| Feed stale or disconnected | Block new entries, retry with back-off, journal the gap, alert | Check internet |
| Broker API unreachable | Block new orders, alert, keep reconciling | Check the broker app |
| Process crash | Supervisor restarts; state rebuilt; **reconcile before trading** | Review the incident |
| Unhedged leg | Retry the hedge once; then follow the group's policy (default: unwind the filled leg); block new entries | Verify in the broker app |
| Reconciliation mismatch | Halt new orders; alert | Resolve manually |
| Margin buffer low | Block entries; alerts at 80% and 90% use | Add funds or reduce |
| Loss limit or drawdown breach | Block entries; persist kill-switch; demote strategy to Paper | Review before restart |
| Token expired | Do not start live; send login ceremony message | Log in |

Per-strategy `outage_policy`: `hold_and_alert` (default for hedged groups), `flatten_if_possible`, or `manual_only`. Shutting the program down never implies positions are closed. Directional strategies should also use resting protective orders at the venue (FR-LIV-9).

### 8.5 Live gates (all required; none met today)

| # | Gate |
|---|---|
| L1 | PRD, architecture and risk profile approved by you |
| L2 | All tests pass with output shown; conformance suites pass for every plugin in use |
| L3 | Strategy promoted through Backtested and Paper with stored evidence (§5) |
| L4 | Venue plugin verified against the broker's current official documentation and conformance suite |
| L5 | Static IP obtained, whitelisted, and verified on the remote runner |
| L6 | Reconciliation, restart recovery and kill-switch rehearsed on the runner |
| L7 | Shadow mode run completed and reviewed |
| L8 | Regulatory and broker requirements re-checked against authoritative sources that day |
| L9 | Capital feasibility and margin rules confirmed for the strategy's instruments |
| L10 | Your explicit written approval for a specific strategy, budget and autonomy level (start small, supervised) |

---

## 9. Documentation system

**In short:** few documents, each starting with a summary that tells the whole story; a one-page index and status; automatic checks keep it that way.

| ID | Rule |
|---|---|
| DOC-1 | Every document starts with **Summary** (≤ 150 words: purpose, key points, status) and metadata (owner, version, date). This applies to PRD, architecture, plugin READMEs, runbooks, decisions and reports. |
| DOC-2 | **At most 12 living documents** in the repository: `README`, `AGENTS`, `INDEX`, `STATUS`, `PRD`, `ARCHITECTURE` (≤ 2,500 words), `PLUGINS` (how to write one, with templates), `RUNBOOK` (daily routine and incidents as checklists), `RISK` (policy and profiles), `CONFIG_REFERENCE` (generated), `DECISIONS` (log), `RUNNER` (deployment guide). |
| DOC-3 | Per-plugin README ≤ 1 page, starting with its summary and `verified_on` facts. Decision records ≤ 300 words. |
| DOC-4 | `docs/INDEX.md` is generated: every document with its summary. `docs/STATUS.md` is a one-page status (what exists, what is next, open decisions), updated at every milestone. |
| DOC-5 | One home per fact. Link, do not copy. |
| DOC-6 | Docs change in the same change set as the code. Stale documents are deleted, not archived in the repo. |
| DOC-7 | Planning material (chat exports, prompts, drafts) lives in `_workbench/` (git-ignored) and is never used as evidence for audits. |
| DOC-8 | `algodesk docs check` (pre-commit) fails if a document lacks a summary, exceeds its budget, has broken links, or the index is out of date. |
| DOC-9 | Reports and AI-agent reports are summary-first too. |
| DOC-10 | **Module design docs:** before each milestone I write a design of at least 5 pages (architecture, features, scope for improvement, connections to other modules, modern and high-performance practices considered) for your approval. After the module is built, its content is condensed into the module's README (≤ 1 page, summary first) and the long form moves to `_workbench/`. |

---

## 10. Testing and quality gates

**In short:** every plugin kind has a conformance suite; modes are proven equivalent by replay; nothing is "passing" without shown output.

| Level | What | When |
|---|---|---|
| Unit | Fee engine, indicators, risk rules, state machine, config, cache gap logic | Every change |
| Conformance suites | One per plugin kind: broker, venue, fee schedule, indicator, screen, strategy, risk rule, notifier, calendar | Before a plugin is loadable in paper or live |
| Replay equivalence | The same recorded data through backtest and paper gives identical decisions | Before each milestone |
| Architecture tests | CB-9 import rules; no second order path; no stub returning success | Every commit |
| Failure injection | Disconnects, stale data, rejects, partial fills, timeouts, restart mid-order | Before the paper gate |
| Live smoke | Short run against live feeds with `sim` orders | After feed or broker changes |
| Budget and docs checks | `algodesk budget`, `algodesk docs check` | Every commit |

Rules: no claim of success without the command and its output; tests are never weakened to pass; every bug found later gets a regression test.

---

## 11. AI-agent working method

**In short:** small, token-lean task cards; one shared rulebook; tests as the arbiter; skills for repeatable practices; every task ends in a committable state so another model can continue.

### 11.1 Roles
You approve. I own requirements, architecture and gate decisions, write the module designs and task cards, and review evidence. Antigravity (with Claude or Gemini, depending on free tokens) and VS Code AI implement locally. Agents make no architectural decisions: they stop and report when a requirement conflicts, an API is uncertain, a destructive operation is proposed, or scope must change.

### 11.2 Operating files

| File | Purpose | Size limit |
|---|---|---|
| `AGENTS.md` (repository root) | Standing rules for every task. Cross-tool format that Antigravity, Claude Code, Cursor and others read. | ≤ 600 words |
| `skills/<name>/SKILL.md` (installed in `.agents/skills/`, the folder your old repository used) | On-demand procedures: implement, author a plugin, report evidence, write docs, product-manage | ≤ 300 words each |
| Task cards (`_workbench/tasks/`) | One module each; never in `docs/` | ≤ 1,200 words |
| Agent reports | Fixed schema, summary first | ≤ 300 words |

### 11.3 Task card (token-lean standard)
`ID` · `Goal` (one line) · `Reqs` (PRD IDs) · `Read` (paths) · `Touch` (only these paths) · `Do` (as many numbered steps as the module needs, grouped by checkpoints CP1, CP2, …) · `Accept` (commands with expected results) · `Report` (schema name). References use paths and IDs, not pasted text. One module per card (the aim is that one card finishes a module); no fixed line cap.

### 11.4 Consistency across Claude, Gemini and other agents
1. Tests and commands are the arbiter, not the agent's opinion.
2. A fixed report schema makes outputs comparable.
3. No model-specific features: rules sit in `AGENTS.md` and skills, not in chat.
4. A card is built from checkpoints (CP1, CP2, …). After each one the agent runs `scripts/check.sh` and appends `CPn done | next step | files` to `_workbench/handoff.md`, so a different model can resume if free tokens run out. A card always ends in a committable state (the **checkpoint rule**).
5. Every report ends with a handoff block of at most 100 words (state, next step, open issues) so a different model can resume.
6. Ambiguity means stop and ask. No guessing.
7. No new dependencies and no refactors outside `Touch`.
8. Every report carries a COVERAGE line mapping each numbered card step to the tests that prove it (or NOT DONE), so consolidating work into larger cards never silently drops a feature or outcome.

### 11.5 Reuse of the old AlgoDesk code (copying allowed after review)
1. You upload a clean copy of `src/` without `artifacts/` (Q5).
2. I build an inventory (modules, size, dependencies, tests) and rate each item: reuse, adapt or rewrite.
3. Approved items are copied with a provenance header (source path and date), adapted to the new plugin contracts, given tests and checked against the budget.
4. Nothing is copied that breaks CB-3 or CB-5.

Candidates from the earlier audit (the inventory confirms them): the mode-invariant execution layer, the indicator bridge, the expression parser, the data-provider abstraction, backtester order types and trailing stops, screener rule composition, reporting, the session calendar (after fixing its empty holiday list) and the CLI.
Not reused: the duplicate broker clients, the second (live) order path, stub checks that return success, and the planning documents.

### 11.6 Loop
1. I write the module design (5 or more pages) and you approve it.
2. I issue task cards one at a time.
3. The agent works; you run the `Accept` commands.
4. You paste the report here.
5. I review it against the PRD and accept or reject.
6. You commit.

---

## 12. Roadmap and milestones

**In short:** each milestone starts with a design of 5 or more pages for your approval (DOC-10), then task cards, then evidence. Quality first; dates are aims.

| M | Name | Scope | Exit criteria |
|---|---|---|---|
| M0 | Foundations | Repository, `AGENTS.md` and skills, config loading, central parameter registry, minimal plugin registry, `doctor`, `budget`, `docs check`, journal skeleton, CLI skeleton | Checks pass; documents have summaries |
| M1 | **Walking skeleton** | `yfinance` data plugin (history); `upstox` plugin (instruments, live stream, basic history); minimal data service with tick and bar recording; indicators SMA, EMA, RSI; one demo strategy; order state machine; `sim` venue; core risk (kill-switch, a few rules); minimal fee schedule; journal and `SUMMARY.md`; Telegram notifications; NSE calendar | **One full paper session on live Upstox data**, a saved summary report, no unhandled exceptions, conformance tests for the plugin kinds used |
| M2 | Data layer | Cache, gap-fill, validation, quarantine, instrument mapping and diffs, accumulation job, `data verify`, BSE and MCX calendars | FR-DAT P1 acceptance passes |
| M3 | Indicators and screens | Full built-in set, custom-indicator scaffold, TOML and Python screens, saved screen runs | A real screen runs on 500 symbols within budget |
| M4 | Backtester and reports | Backtest mode (bar and tick level), models, portfolio mode, results store, compare | Replay-equivalence test passes; reports reproducible |
| M5 | Risk and control | Risk profiles in the registry, monitors, health guards, Telegram commands, outage policies | Failure-injection suite passes |
| M6 | Zerodha plugin and shadow | Zerodha venue (disabled by default), all order types, reconciliation, fee schedules, shadow mode | Conformance suite passes; shadow run reviewed |
| M7 | Remote runner | Profile B on the static-IP host, login ceremony, probe, supervisor, backups | Latency numbers measured; recovery drills pass |
| M8 | Extended paper | Campaign per promoted strategy | Plan criteria met |
| M9 | Micro-live | Live gates §8.5, smallest size, supervised (L1) | Your written approval |

**Schedule.** Today is Saturday 17 October 2026. My earlier drafts had the date wrong (they said 12 and 13 October), so the "Friday 16 October" aim had already passed; it is dropped. Quality comes first and dates are aims, not promises. Aims: the M0 design was approved on Sat 17 Oct and T0-1 starts now; M0 is done about Fri 23 Oct; the M1 walking skeleton about Fri 30 Oct. NSE holidays in this window must be checked (N-7). M0 plus M1 is roughly 8–10 working days with AI agents.

**Candidate first plugins after M1** (each with its own one-page spec in its folder, not in this PRD): cash–futures arbitrage (spec seeded from PRD v1.0 §6), an opening-range-breakout strategy, and a trend-template screen.

---

## 13. Cost register

**In short:** ₹0 recurring until you approve. The one open cost is the static-IP host, needed only from M7.

| Item | Cost | Status |
|---|---|---|
| Python, Git, SQLite, Parquet, Telegram bot, VS Code | ₹0 | Free |
| AI tools (Antigravity free, Claude free, ChatGPT Go, Gemini Pro) | Already held | Existing |
| `yfinance` data | ₹0 | Personal use per Yahoo's terms (§4.3) |
| Zerodha Kite Connect Personal plan (orders) | ₹0 | Verified |
| Zerodha Connect plan (adds data) | ₹500 per month per key | Verified; **not needed** while Upstox supplies live data |
| Upstox API access | Unknown | Open: check your dashboard (N-4) |
| Static-IP host | Oracle Always Free ₹0 (lead candidate); AWS credits are temporary; VPS, proxy or VPN are not ₹0 | §6.3; decision at M7 |
| Trading charges (brokerage, STT, etc.) | Per trade | Defined as fee-schedule data |

Rule (D-14): nothing paid without your explicit approval after verified results.

---

## 14. Risks

| ID | Risk | Mitigation |
|---|---|---|
| R-1 | Complexity creep like old AlgoDesk | Budget, architecture tests, admission rule (§2.2) |
| R-2 | Backtest and live diverge | One engine, replay-equivalence tests |
| R-3 | Overfitted backtests | Out-of-sample discipline, trial counts in reports |
| R-4 | AI agents invent APIs or claim untested success | Official-docs verification, evidence rule, conformance suites |
| R-5 | Static IP and cloud cost surprises | §6.3 options; measure with `probe` before committing |
| R-6 | Daily broker login friction | Login ceremony via Telegram; no credential automation |
| R-7 | Broker or rule changes | Facts carry dates; re-verify at gates; plugins isolate change |
| R-8 | Poor data quality | Validation, quarantine, lineage |
| R-9 | Schedule pressure leads to shortcuts | Gate rules; slip the date, not the safety |
| R-10 | Paper results flatter reality | Conservative fills, explicit disclaimers |
| R-11 | Runner too small for the engine | 512 MB budget, lean live path |
| R-12 | Single point of failure (one person, one runner) | Runbook, manual failover, alerts |

**Change control:** any scope, architecture or budget change needs a reason, alternatives, affected IDs and your approval, and is recorded in the decision log with a version bump.

---

## 15. Open decisions

**In short:** none block M0 or M1. Your answers of 17 Oct are recorded in Appendix A (D-31 to D-34).

| Topic | Status |
|---|---|
| Config format | Decided: TOML |
| yfinance in paper and live | Decided: explicit `live_suitable` source (Upstox); yfinance only through a recorded override |
| Order types | Decided: staged; bracket platform-emulated |
| Static IP host | Decided: Oracle Always Free account created at M7, not before; AWS credits remain the fallback |

---

## 16. Approval checklist

- [ ] Vision, eleven capabilities and non-goals (§1)
- [ ] Principles and complexity budget (§2)
- [ ] Architecture, plugin kinds, modes and rules (§3)
- [ ] Capability requirements (§4) and lifecycle (§5)
- [ ] Deployment profiles and runner requirements (§6)
- [ ] Non-functional requirements (§7)
- [ ] Safety invariants, risk profile, outage policy and live gates (§8)
- [ ] Documentation system (§9)
- [ ] Testing and AI-agent method (§10–§11)
- [ ] Roadmap, honest schedule and cost register (§12–§13)

**Requested next action:** none; follow the current task card.

---

## Appendix A — Confirmed decisions register

**In short:** what you have decided so far, in one table.

| ID | Decision |
|---|---|
| D-01 | Start afresh as AlgoDesk Next in a completely separate repository. Good code and practices from the old AlgoDesk may be copied after review (17 Oct); nothing is copied blindly. |
| D-02 | Local-first. MacBook is the primary machine; a backup process exists for failures. |
| D-03 | Upstox supplies market data; Zerodha places orders. Both are plugins. |
| D-04 | AlgoDesk Next is a platform: any broker, strategy, indicator, screen and fee schedule is defined separately as a plugin. |
| D-05 | Long or short, holding period, instruments and overnight holding are strategy-level choices, not core constraints. |
| D-06 | Risk limits are percentage-based and centrally configurable; I propose numbers, you approve. |
| D-07 | A download-and-cache data layer: no re-downloading, staleness and correctness checks, incremental appends. |
| D-08 | Upstox and Zerodha API subscriptions exist. |
| D-09 | Static IP for orders by any method that meets Zerodha's rule. Priorities: zero cost first, then lower latency. You have no cloud account yet. Broker-specific, outside the core, and must add no extra latency (§6). |
| D-10 | Telegram alerts accepted; a paper-trading period with pass criteria precedes live trading. |
| D-11 | A deadline for paper trading is an aim, not a requirement: quality first (17 Oct). |
| D-12 | Tax ledger export is outside phase 1. |
| D-13 | PRD delivered as Markdown. |
| D-14 | Capital protection first. Live only after audit, design, tests, backtests, paper trading and your explicit approval. Paid services only after verified results and your approval. |
| D-15 | Planning capital range ₹1–5 lakh (planning only). |
| D-16 | All documentation must be short and carry a summary that conveys everything in brief (§9). |
| D-17 | Carried over from old AlgoDesk: single retail user; Python with TOML configuration (updated 17 Oct); CLI plus Telegram; browser UI deferred. |
| D-18 | v1 strategies are written in Python. |
| D-19 | All order types Zerodha offers are in scope (market, limit, stop, stop-market, iceberg, GTT, bracket, cover, AMO). |
| D-20 | All tradable instruments on Upstox and Zerodha across NSE and BSE equity, F&O and MCX; the universe is chosen per strategy or screen. |
| D-21 | Data in v1: daily, 1-minute and live ticks; tick-level backtesting wanted. |
| D-22 | Risk profile numbers and the complexity budget are approved, but must be centrally registered and changeable at any time, never hard-coded. |
| D-23 | Documentation rules approved (summary in every document, 12-document cap). |
| D-24 | yfinance is part of v1 with its full capability; you can name a data source per run, otherwise the default is yfinance. |
| D-25 | Before any module is built, Claude provides a design of at least 5 pages: architecture, features, scope for improvement, connections, and modern, HFT and enterprise practices. |
| D-26 | Development is mostly in Antigravity, with the agent switching between Claude and Gemini by free tokens: prompts and tasks must be token-lean and give consistent output across agents. |
| D-27 | Claude provides skills for coding, project management and other practices. |
| D-28 | Code-size numbers (about 20,000 core lines, about 2,500 per plugin) are guidance, not hard limits: keep code minimal but never at the cost of quality, documentation or speed; it may grow slightly if needed. |
| D-29 | Python 3.13, installed and pinned with `uv`, chosen on your request for speed, library compatibility and cloud portability (§3.8). |
| D-30 | M0 design approved: `uv`, a free private GitHub repository for code backup, the parameter change rules (tighten now, loosen gated) and the ten-task breakdown. Old AlgoDesk code uploaded; reuse inventory delivered. |
| D-31 | Config, manifests, parameter defaults and fee schedules use TOML. |
| D-32 | Live data always comes from Upstox until another broker is configured. yfinance is valued for adjusted OHLCV in backtests. Paper and live require an explicit `live_suitable` source; yfinance only through a recorded override. |
| D-33 | Order types are built in stages and bracket orders are platform-emulated (Zerodha has no native bracket order today). |
| D-34 | The Oracle Cloud account is created only at the stage it is needed (M7). |
| D-36 | Everything stays local: all libraries, Python and caches live only in the project folder and its virtual environment; nothing is installed globally; the MacBook's system folders must never install or load anything from the project; testing only in the virtual environment (17 Oct). |

---

## Appendix B — Glossary

| Term | Meaning |
|---|---|
| Plugin | A self-contained add-on (broker, strategy, indicator and so on) loaded by name from config |
| Core | The small, stable program that runs plugins |
| Capability | A declared thing a plugin can do (for example `orders`) |
| Mode | Research, backtest, paper or live |
| Venue | The thing that executes orders: the simulator or a real broker |
| Intent | A strategy's request, such as "buy X"; risk and the order manager decide what happens |
| Order group | Several linked orders (legs) treated as one unit |
| Backtest | Testing a strategy on past data |
| Paper trading | Simulated orders on live data |
| Look-ahead | Accidentally using future data in a test (invalid results) |
| Walk-forward / out-of-sample | Testing on data the strategy was not tuned on |
| Reconciliation | Comparing the program's records with the broker's |
| Kill-switch | Blocks all new trades immediately |
| Static IP | An internet address that never changes; Zerodha requires one for order APIs |
| Egress proxy | A relay on another machine through which calls leave the network |
| Fail closed | When unsure, do nothing risky |
| ADR | A short decision record |

---

## Appendix C — Example plugin manifest and run configuration (illustrative)

```toml
# plugins/venues/zerodha/plugin.toml
name = "zerodha"
kind = "venue"
version = "0.1.0"
api_version = 1
entry = "plugin:ZerodhaVenue"
order_capable = true            # only loadable in live mode
modes_allowed = ["live"]
verified_on = 2026-10-17
sources = ["support.zerodha.com", "kite.trade/forum"]

[capabilities]
orders = true
positions = true
funds = true
gtt = true

[limits]
orders_per_second = 10
orders_per_minute = 200
orders_per_day = 3000

[requires]
static_ip = true
login = "interactive_daily"
```

```toml
# config/runs/demo_paper.toml
allocated_capital = 100000

[run]
name = "demo_paper"
mode = "paper"

[data_source]
plugin = "upstox"               # paper and live need a live_suitable source

[execution_venue]
plugin = "sim"
fill_model = "conservative_book"
latency_model = "measured"

[fees]
schedule = "zerodha_nse"

[calendar]
plugin = "nse"

[secrets]
plugin = "keychain"

[deployment]
profile = "local"

[universe]
screen = "nifty_liquid_100"

[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
params = { fast = 20, slow = 50, timeframe = "5m" }

[[notifiers]]
plugin = "telegram"
heartbeat_minutes = 15
```

---

## Appendix D — Plugin contracts and reference plugin summaries

### D.1 Contracts (sketch; exact signatures are fixed with each kind's first real plugin, from M1 on, and then versioned)

| Kind | Core methods |
|---|---|
| Broker / DataSource | `instruments()`, `history(series, start, end)`, `stream(subscriptions)` → async events, `limits`, `auth` (AuthFlow) |
| AuthFlow | `begin()` → login request, `complete(code)` → session, `validate(session)`, `expires_at(session)` |
| ExecutionVenue | `place(order_spec, client_ref)`, `modify`, `cancel`, `order(client_ref)`, `orders()`, `positions()`, `funds()`, `supports(capability)` |
| FeeSchedule | Data file; engine call `charges(fill)` → breakdown |
| Indicator | `spec` (inputs, params, outputs, warm-up), `compute(series)`, `init_state()`, `update(state, bar)` |
| Screen | `spec`, `run(universe, as_of)` → table plus summary |
| Strategy | `params_schema`, `on_start`, `on_bar`, `on_tick`, `on_fill`, `on_timer`, `on_stop`, `state()` / `restore(state)` |
| RiskRule | `check(intent, view)` → allow or reject with reason; `monitor(view)` → actions |
| Notifier | `send(message, severity)`, `commands()` → async inbound commands |
| Calendar | `is_trading_day(date)`, `sessions(date)`, `next_session(ts)` |
| SecretsProvider | `get(name)`, `set(name, value)` |
| Transport | `session()` returning the HTTP session used by a broker plugin |

### D.2 Reference plugins for v1 (facts verified 17 Oct 2026 unless marked; each lives in its own README)

| Plugin | Summary and constraints |
|---|---|
| `brokers/yfinance` (data; default source) | Unofficial Yahoo Finance access, personal use per Yahoo's terms. Symbols `.NS` (NSE) and `.BO` (BSE). Intraday history is short: 1-minute bars only for about the last 7 days, 2 to 90 minute bars about 60 days, hourly about 730 days; daily and longer reach much further. Also splits and dividends, fundamentals, company info, options chains. No official rate limit; throttles with HTTP 429 (keep to about 2 requests per second); cookie handshakes break now and then. NSE and BSE quotes can be delayed (secondary sources say about 15 minutes; **Not verified**). No tick history. A Yahoo WebSocket exists but is unofficial. Declared `live_suitable: false`. Facts come from secondary sources (VF-14). |
| `brokers/upstox` (data) | Market Data Feed V3 over WebSocket with Protobuf. Normal limits: 2 connections; `full` mode up to 2,000 instrument keys (1,500 when combined with other modes); `ltpc` up to 5,000. `full` mode includes last price, 5 levels of depth, volume, open interest and 1-minute candles. First message is market status, second a snapshot. Historical V3: 1–15 minute candles from January 2022, one month per request. Access token reportedly expires about 03:30 IST daily (**Not verified**). |
| `venues/zerodha` (orders and its own login flow) | Free Personal plan covers orders (no data); ₹500/month Connect plan adds data. No sandbox. Order varieties: regular, amo, co (cover), iceberg (2–10 legs), auction. Order types: MARKET, LIMIT, SL, SL-M. Validity: DAY, IOC, TTL. Products include CNC, MIS, NRML and MTF. GTT (single and two-leg) uses separate endpoints. The official variety list has no bracket order (**N-14**). Static IP whitelisting for order endpoints (§6.3). Market and SL-M orders need non-zero market protection (-1 means automatic); it does not apply to LIMIT or SL. MCX: no IOC for algo orders. Slicing capped at 10. Limits: 10 orders per second per client id (429 above), 200 per minute, 3,000 per day. Strategies under the exchange threshold of 10 orders per second need no registration. Daily interactive login. |
| `fees/zerodha_nse` (data) | Delivery: brokerage ₹0, STT 0.1% on buy and sell, stamp duty 0.015% on buy, NSE transaction charge 0.00297%, DP charge ₹13.5 plus GST on sells. Intraday: brokerage ₹20 or 0.03% (lower), STT 0.025% on sell, stamp 0.003% on buy. Futures: brokerage ₹20 or 0.03% (lower), STT 0.02% on sell, transaction charge 0.00173%. SEBI fee ₹10 per crore; GST 18% on brokerage, SEBI and transaction charges. Options, currency, commodity and futures stamp duty to be added and verified when needed. |
| `venues/sim` | Conservative book-walking fills, latency model, partial fills, failure injection, virtual account; implements every order type in the neutral `OrderSpec` with documented semantics. |
| `notifiers/telegram` | Bot HTTPS API with long-polling; authorized chat ids only. |
| `calendars/{nse,bse,mcx}` | NSE and BSE equity 09:15–15:30 IST (pre-open from 09:00). MCX sessions run into the late evening (exact timings loaded from official data; **Not verified**). Holidays are loaded from official lists, never hard-coded (N-7). |
| Candidate strategies | `basis_arbitrage` (spec seeded from PRD v1.0 §6; open issues: lot size vs capital, settlement mechanics), `orb`, trend-template screen. Each gets a one-page spec in its folder. |

---

## Appendix E — Sources and fact register (checked 17 October 2026)

**Verified**

| ID | Fact | Source |
|---|---|---|
| VF-1 | Zerodha API plans and prices | support.zerodha.com: "What are the charges for Kite APIs" and "Kite Connect API FAQs" |
| VF-2 | No Kite Connect sandbox | Same FAQ page |
| VF-3 | Static IP rules (whitelist on the developer profile, order endpoints only, ISP, cloud or VPN source, IPv4 or IPv6 matching the real egress, one IP per developer account, family sharing in one profile) | Kite developer forum, Zerodha team posts in thread "Preparing to comply with SEBI's retail algo rules (Static IP, ratelimits, order types)" |
| VF-4 | Market protection for market and SL-M orders (non-zero, -1 automatic; not for LIMIT or SL); MCX included; MCX no IOC; slicing cap 10 | Same thread |
| VF-5 | Order limits: 10 per second per client id, 200 per minute, 3,000 per day | Same thread; kite.trade/forum (comment 46596) |
| VF-6 | Exchange registration only above 10 orders per second | zerodha.com/z-connect: "Explaining the latest SEBI algo trading regulations" |
| VF-7 | Upstox Market Data Feed V3 limits, modes, message order | upstox.com/developer/api-documentation/v3/get-market-data-feed |
| VF-8 | Upstox Historical Candle V3 intervals, availability, window | upstox.com/developer/api-documentation/v3/get-historical-candle-data |
| VF-9 | Zerodha charges | zerodha.com/charges and zerodha.com/z-connect/?p=391776 |
| VF-10 | Zerodha order varieties (regular, amo, co, iceberg, auction), order types, validity, iceberg 2–10 legs | kite.trade/docs/connect/v3/orders (official); product list from client libraries (secondary) |
| VF-11 | AWS new accounts (on or after 15 July 2025): credits and a 6-month Free plan, credits expire at 12 months; older accounts keep the 12-month offer | builder.aws.com article "What AWS actually gives you for free in 2026", confirmed by several independent summaries |
| VF-12 | Public IPv4 priced at $0.005 per hour | Same AWS article and oneuptime.com AWS Free Tier guide |
| VF-13 | Oracle Always Free: two AMD micro VMs (1 OCPU, 1 GB, one public IP, up to 50 Mbps each) and Arm A1 Flex (3,000 OCPU-hours and 18,000 GB-hours per month), in your home region; reserved public IPs are free while attached (secondary sources say one or two) | docs.oracle.com Always Free Resources; secondary guides |
| VF-14 | yfinance limits (1-minute about 7 days, 2–90 minute about 60 days, hourly about 730 days), no official rate limits, HTTP 429 throttling, personal-use terms | Secondary: library and tool documentation, 2026 |
| VF-15 | Antigravity reads `AGENTS.md` (cross-tool) and `GEMINI.md` (Antigravity-only, higher precedence); skills are directories with a `SKILL.md`, in a workspace or global skills folder (folder name differs between sources) | Google codelab "Authoring Google Antigravity Skills"; vendor guides |
| VF-16 | Index derivative contract value rules (context only) | NSE circular FAOP64625 |

**Not verified (do not rely on)**

| ID | Item | How to verify |
|---|---|---|
| N-1 | Stock-futures lot values (a secondary source says about ₹5–10 lakh) | NSE F&O contract file |
| N-2 | Physical settlement mechanics and extra STT or margin near expiry | NSE and Zerodha documentation |
| N-3 | Upstox read-only "analytics token" and the exact daily token expiry time | Test in M1 |
| N-4 | Upstox API fee, if any | Your Upstox dashboard |
| N-5 | Expired-contract historical data availability and cost | Upstox docs |
| N-6 | Zerodha margin treatment of hedged positions and pledged holdings | Margin calculator and support pages |
| N-7 | NSE, BSE and MCX holidays for the coming weeks | Official holiday lists |
| N-8 | Reliability of IPv6 egress for order calls | Test |
| N-9 | Mumbai-region AWS instance price and whether credits cover about 12 months | AWS pricing calculator |
| N-10 | Latency from home and from a Mumbai host to Upstox and Zerodha endpoints | probe tool (DEP-8, M7) |
| N-11 | ISP static-IP availability and price | Your ISP |
| N-12 | Oracle: Mumbai home-region availability, Arm capacity, card verification for Indian cards, idle-instance reclamation rules | Oracle signup and documentation |
| N-13 | yfinance delay for NSE and BSE quotes and its current stability | Live test in M1 |
| N-14 | Whether Zerodha offers any native bracket order today | Zerodha support |
| N-15 | GTT semantics (products, validity, intraday use) and iceberg rules per exchange | Zerodha docs and tests |
| N-16 | Antigravity skills folder name | Resolved: your old repository used `.agents/skills`; a `.agent` alias link covers the other spelling |

---

## Appendix F — Changes from v2.8

| Topic | v2.8 | v2.9 |
|---|---|---|
| Card size | At most 800 words | At most 1,200 words (module-sized cards need the detail; checkpoints and COVERAGE keep them reliable) |
