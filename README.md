## Summary

AlgoDesk Next is a pluggable trading workbench for Indian markets. It provides a plugin-based architecture with typed interfaces, layered configuration, a central parameter registry with audit trail, an append-only journal, structured logging with secret scrubbing, and a CLI with operational health checks. The project enforces strict quality gates — linting, type checking, testing and architecture rules — through a single command. It is designed for research, backtesting, paper trading and (eventually) live execution, with safety invariants enforced at every layer.

### Quick start

```bash
source scripts/env.sh
uv sync
bash scripts/check.sh
```

Run `source scripts/env.sh` in every new terminal.
Nothing is installed outside this folder.

### CLI Commands and Exit Codes

AlgoDesk provides a unified command-line workbench:

- `algodesk version`: Print version information.
- `algodesk doctor [--verbose] [--only ID]`: Run diagnostic health checks across the platform and plugins.
- `algodesk config show`: Display effective platform and run configuration.
- `algodesk param show [PREFIX]`: Display registered parameters and active values (`--json` supported).
- `algodesk param set NAME VALUE --reason R [--scope S] [--confirm PHRASE]`: Override parameter value with audit trail.
- `algodesk param history [NAME] [--limit]`: Inspect parameter change audit log.
- `algodesk param reset NAME --reason R`: Reset parameter override back to shipped default.
- `algodesk plugins list`: List discovered plugins with conformance status (`--json` supported).
- `algodesk plugins check`: Validate plugin manifests and structures.
- `algodesk plugins test KIND NAME`: Run plugin test suite and base contract checks, recording conformance.
- `algodesk new plugin KIND NAME`: Scaffold a new plugin skeleton.
- `algodesk journal verify`: Verify cryptographic hash chain integrity of the append-only journal.
- `algodesk journal tail [-n N]`: Display recent events from the append-only journal.
- `algodesk journal runs`: List execution runs recorded in the journal.
- `algodesk budget [--json]`: Evaluate codebase size and dependency complexity against budget.
- `algodesk docs check`: Validate documentation consistency, summary sections, and relative links.
- `algodesk docs index`: Regenerate deterministic living documentation index docs/INDEX.md.
- `algodesk lint [--json]`: Run AST architecture rules over core code and plugins.
- `algodesk secrets scan`: Scan codebase for exposed secrets, tokens, and cloud keys.
- `algodesk data get SYMBOL TIMEFRAME START END`: Retrieve historical bar slices with rate-limiting, caching, dynamic backward adjustment, and resampling.
- `algodesk data accumulate [--group G] [--source S] [--days N] [--dry-run]`: Fetch completed historical sessions (1m/1d) for groups, journal progress, and report missing/lost days.
- `algodesk data compare --date D [--symbols A,B] [--source S]`: Rebuild 1m bars from recorded ticks and compare close/volume differences against source bars.
- `algodesk data delete SELECTOR --kind <bars|ticks> [--older-than N] --reason R`: Safely delete data with timestamped trash; typed confirmation required for ticks; `--purge` blocked on ticks.
- `algodesk data import PATH --mapping FILE [--dry-run]`: Ingest vendor CSV or Parquet files through schema validation and stores with SHA-256 provenance.
- `algodesk data smoke [--symbols A,B]`: Live smoke verification testing zero network requests on run 2, gaps, manifest integrity, and cross-source diff.
- `algodesk data verify`: Validate SHA-256 checksums, row counts, and schema conformance across datasets.
- `algodesk data status`: Report dataset counts, bar/tick partition files, row totals, disk storage, and quarantine stats.
- `algodesk screen run NAME [--group G] [--as-of D] [--timing]`: Run market screen with honest skip reasons, splits-basis default, and optional throughput timing breakdown.
- `algodesk screen list`: List configured TOML screens and discovered Python screen plugins.
- `algodesk screen show NAME`: Display full screen specification and rules.
- `algodesk groups import PATH --name N`: Ingest current index constituent CSV mapping ISIN to symbol.
- `algodesk groups import-history PATH --name N --index I [--strict]`: Ingest constituent history timeline with symbols as authority and sidecar inconsistency tracking.
- `algodesk groups show NAME [--as-of D] [--resolved]`: Query point-in-time index membership and coverage metrics.

#### Exit Codes

- `0`: Success (all checks passed, command completed).
- `1`: Handled domain error (`AlgoDeskError`), verification failure, or warning exit in `doctor`.
- `2`: Usage error (invalid syntax, missing required options) or critical failure in `doctor`.
- `70`: Internal error / unhandled bug (`--debug` prints traceback).

### Living Documents

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): Strict layer architecture, package responsibilities, and dependency rules.
- [`docs/DECISIONS.md`](docs/DECISIONS.md): Architectural decision records capturing context and rationale.
- [`docs/INDEX.md`](docs/INDEX.md): Living documentation index generated by `algodesk docs index`.
- [`docs/PLUGINS.md`](docs/PLUGINS.md): Guide to creating, testing, and conforming plugin packages.
- [`docs/PRD.md`](docs/PRD.md): Product requirements document and safety invariants for milestones M0 to M9.
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md): Operating procedures, diagnostic workflows, and environment setup checklists.
- [`docs/STATUS.md`](docs/STATUS.md): Progress status, milestone deliverables, and open decisions.
