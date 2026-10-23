# Decisions

Owner: Aakash · Version: 1.0 · Date: 19 October 2026

## Summary

Short decision records (context, decision, reason) in the order they were taken. They explain why the code looks the way it does. Each stays under 80 words; larger designs live in the PRD. Add a new record whenever a choice changes structure, tooling or a rule; never edit history, add a record that supersedes it.

## Records

**ADR-001 Python 3.13 managed by uv.** Mature, widely supported, portable to any server; uv pins the same interpreter everywhere. 3.14 revisited at M7.

**ADR-002 TOML for configuration, manifests, parameter defaults and fee schedules.** Built into Python 3.13, no extra package, comments allowed.

**ADR-003 Local isolation.** Python, libraries, caches and hook environments live under `.venv/` and `.local/`; `uv` is configured for manual Python downloads; `doctor` and `check.sh` verify it.

**ADR-004 Per-kind plugin interfaces are deferred.** They are fixed with each kind's first real plugin so they follow real needs, not guesses.

**ADR-005 `pydantic` with strict models for configuration and manifests.** Unknown keys are errors; messages name the file and key.

**ADR-006 SQLite (WAL) for parameters and journal.** Zero cost, one file each, transactional, ships with Python.

**ADR-007 Hash-chained, append-only journal.** Tamper evidence is cheap now and hard to add later; durable events are synced before returning.

**ADR-008 Shared `Scrubber` in `api`.** Journal and logs must not import each other; both mask secrets through one class.

**ADR-009 Conformance records keyed to code hash.** Editing a plugin invalidates its pass; paper and live need a current pass.

**ADR-010 Parameter changes: tighten at once, loosen gated.** Loosening while a live strategy is armed needs a typed confirmation and waits for the next session.

**ADR-011 Size numbers are advisory guidance.** Quality, documentation and speed come first; dependency counts and structural rules are hard.

**ADR-012 Developer tools live in `src/algodesk/devtools/`.** A top-level folder is not importable from the installed package.

**ADR-013 One module per task card, with checkpoints and a handoff file.** Fewer, larger cards; work can resume with another AI model.

**ADR-014 Zerodha is a venue plugin; brokers are data sources.** The Zerodha plugin also holds its own login flow.

**ADR-015 yfinance is the default research data source, never a live source.** Live and paper need an explicit `live_suitable` source (Upstox); yfinance only by a recorded override.

**ADR-016 Static IP host decided at M7.** Oracle Always Free is the lead candidate; AWS credits are the fallback; no account is created before then.

**ADR-017 Size guidance raised to 20,000 core lines, 100 modules, 2,500 per plugin.** M0 alone was 5,445 lines (about three times the early estimate) because validation, messages and tests were written carefully; size stays advisory.

**ADR-018 Shipped parameter defaults are authoritative.** The database stores overrides and history only; a changed default never breaks startup.

**ADR-019 Parameter and journal writes are transactional and fail closed.** A journal write failure stops all further appends until reopened.

**ADR-020 Public API now holds market and order types; CB-6 raised to ~60 types.** As M1 introduces canonical market and trading data representations across plugins and core, `algodesk.api` defines frozen domain types (Instrument, Quote, Bar, Order, Fill, Position, Portfolio) and Protocols. CB-6 guidance is raised from ~25 to ~60 public exports to accommodate these types while keeping logic out of `api`.

**ADR-021 Market hours are central registry parameters; sessions are data, MCX has two.** Session opening, closing, square-off times and weekdays are defined in `config/params/markets.toml` under the parameter registry, enabling runtime adjustment. Calendar plugins provide exchange-specific trading day status, holidays, and per-session closures (MCX supports morning and evening sessions).

**ADR-022 Fees are dated data read by one engine.** Fee schedules are TOML data files specifying dated component rates (flat, percentage, capped, per-crore, DP per-scrip-day) and GST rules. A single core `FeeEngine` computes charges deterministically across backtest, paper, and live modes rather than writing custom fee code per broker.
