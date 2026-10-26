# Architecture

Owner: Aakash · Version: 1.0 · Date: 19 October 2026

## Summary

AlgoDesk Next is a small core plus plugins. The core is ten packages in strict layers: `api` (contracts, no internal imports) at the bottom; `fees`, `config`, `params`, `registry`, `journal` and `logs` above it, independent of each other; `ops` (composition root, doctor) and `devtools` (budget, docs, lint, secret scan) above those; `cli` on top. Plugins import only `algodesk.api`; the core never imports a plugin. Every adjustable number lives in the parameter registry, every event in a hash-chained journal, and every plugin load passes mode, capability and conformance gates. Import rules are enforced by tests. Market data, strategies, orders and brokers arrive from milestone M1 onward as plugins.

## 1. Layers

```
cli
ops | devtools
fees | config | params | registry | journal | logs   (independent of each other)
api                                                    (imports no other algodesk package)
plugins/  ->  algodesk.api only
```
Enforced by `import-linter` contracts in `.importlinter` (run by `scripts/check.sh`) and by `algodesk lint`.

## 2. Packages

| Package | Purpose | Main public names |
|---|---|---|
| `api` | Contracts, domain types and protocols | `Instrument`, `Quote`, `Bar`, `Order`, `Fill`, `Position`, `Portfolio`, `Indicator`, `Calendar`, `PluginBase`, error classes |
| `fees` | Deterministic fee calculation and schedules | `FeeEngine`, `Schedule`, `charges_for_fills` |
| `config` | Layered TOML configuration, validated | `PlatformConfig`, `RunConfig`, `load_platform`, `load_run`, `build_effective`, `render_effective`, `resolve_paths`, `ensure_dirs` |
| `params` | Central parameter registry (SQLite) | `ParamRegistry`, `ParamSpec`, `load_defaults`, `ChangeResult` |
| `registry` | Plugin discovery, validation, gating, loading | `PluginRegistry`, `discover`, `code_hash`, `ConformanceStore`, `scaffold_plugin`, `base_contract_problems` |
| `journal` | Append-only hash-chained event log (SQLite) | `Journal`, `JournalSettings`, `verify_journal` |
| `logs` | JSON logging with secret scrubbing | `setup_logging`, `get_logger` |
| `ops` | Wiring and diagnostics | `open_runtime`, `Runtime`, `DoctorRegistry`, `build_core_checks` |
| `devtools` | Automatic fences | budget, docs, archlint, secretscan, `build_tool_checks` |
| `cli` | The `algodesk` command | Typer app |

## 3. Start-up sequence (`open_runtime`)

1. Find the project root; read `config/platform.toml` and the other layers (platform, profile, run, environment, command line); validate; stop with a three-line error on any problem.
2. Resolve paths; they must stay inside the project folder. Directories are created only when asked.
3. Open the parameter registry (only if its database exists or creation is requested) and load defaults from `config/params/*.toml`.
4. Discover plugin manifests without importing any plugin code.
5. Commands then run; long-running processes (from M1) also open the journal and start logging.

## 4. Where things live

| Path | Content |
|---|---|
| `config/` | `platform.toml`, `profiles/`, `runs/`, `params/*.toml` defaults, lint and scan rules and their allow-lists |
| `data/state/params.db`, `data/state/journal.db` | Parameter overrides and history; journal (git-ignored) |
| `logs/algodesk.log` | JSON log lines, rotated daily (git-ignored) |
| `.algodesk/conformance.json` | Conformance pass records keyed by plugin code hash |
| `.venv/`, `.local/` | Virtual environment, project-local Python and caches (isolation rule) |
| `_workbench/` | Scratch: task cards, handoff file, design drafts (never in docs) |

## 5. Safety rules already implemented

- An order-capable plugin is refused in research, backtest and paper **before its code is imported**; in live it needs a current conformance record.
- Paper and live loads require a conformance record for the plugin's current code hash.
- Undeclared capabilities raise `NotSupported`; there are no stubs that return success (checked by `algodesk lint`).
- The journal forbids updates and deletes, chains every event by hash, and `journal verify` detects edits, removals and broken links.
- Secrets are references (`secret:<name>`); plain secrets in configuration are rejected; logs and journal payloads pass through the scrubber.
- Parameter changes need a reason; tightening applies at once; loosening while a live strategy is armed needs a typed confirmation and waits for the next session.
- The environment must be isolated: `doctor` and `check.sh` fail if Python, caches or packages come from outside the project folder.

## 6. Plugin loading

`PluginRegistry.create(kind, name, config=..., ctx=...)` checks, in order: the plugin exists; the mode is allowed by its manifest; order-capable plugins only in live (before import); a current conformance record for paper and live; the configuration against the plugin's `ConfigSchema`; then a lazy import under a unique module name, instantiation with `(manifest, config, ctx)`, and a final check that the object satisfies the `Plugin` contract.

## 7. Quality gates (`bash scripts/check.sh`)

isolation, ruff check, ruff format check, mypy (strict), pytest, import-linter contracts, budget (size advisory, dependencies hard), docs check, architecture lint, secret scan. The last line is `CHECK: PASS` or `CHECK: FAIL (n)`.

## 8. Market Sessions & Calendars

Market sessions (open, close, square-off, weekdays) are centralized as parameters under `market.<exchange>.<segment>` in the parameter registry (`config/params/markets.toml`). Calendar plugins read registry parameters for timing while supplying exchange-specific trading days, holidays, per-session closures (closed = all, morning, evening; MCX has two sessions), and special session timings from data files.

## 9. Secrets & Broker Ingestion

Secrets providers (`keychain`, `env`) expose name-based lookup through `SecretReader`. In `build_context`, all retrieved secret values are registered with `Scrubber` to guarantee redaction in logs and journals. Broker plugins implement `DataSource` (historical bars, real-time tick/quote feeds) and `AuthFlow` (interactive browser login or analytics tokens).

## 10. Not built yet

The clock, order manager, portfolio engine, execution engine, demo strategy runner, Telegram notifier, and remote runner.
