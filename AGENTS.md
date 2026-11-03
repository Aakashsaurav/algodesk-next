# AGENTS.md — rules for every AlgoDesk Next task

## Summary
You are an implementation agent on AlgoDesk Next, a pluggable trading workbench for Indian markets. Do exactly what the task card says, only in the paths it allows, with tests as proof. Stop and ask when unsure. Never place live orders, never touch secrets, never invent broker APIs. Finish every task in a committable state and end with the report and handoff block.

## Before you start
1. Read the task card, then only files in its `Read` line. If `_workbench/handoff.md` exists, read its last 25 lines only (`tail -n 25 _workbench/handoff.md`).
2. Inspect existing code before writing. Reuse; one implementation per concern.
3. If a requirement is unclear, conflicting, or needs a new dependency: STOP and ask.

## Hard rules
- Touch only paths in `Touch`. No refactors elsewhere.
- `src/algodesk` never imports from `plugins/`. Plugins never import each other. Strategies never import venues or brokers.
- Orders go only through order manager. No live orders in any task.
- No stubs returning success. Unsupported means raise `NotSupported`.
- No hard-coded limits, thresholds or timeouts: read from parameter registry.
- No secrets in code, config, logs, tests or chat. Use `secret:<name>` references.
- Broker/API facts come only from official docs cited in the card. Never guess.
- No new dependency unless allowed by the card.
- Install nothing outside this project: no `pip install`, `brew`, `npm -g`, `uv tool install` or `sudo`. Run `source scripts/env.sh` at terminal start and prefix commands using `uv` with `source scripts/env.sh &&`. Never run bare `uv`.
- Never weaken or delete a test to make it pass.
- Do not run modifying git commands (`add`, `commit`, `push`, `stash`, `reset`, `checkout`). `git status`, `git diff`, `git log` are fine; owner commits.
- If a file outside `Touch` must change, stop and declare in DEVIATIONS with reason.
- Every Markdown file starts with a Summary of at most 150 words.

## Quality bar
Typed (mypy), linted (ruff), tested (pytest with failing-case test), functions at most 50 lines, actionable error messages (what, where, fix). Money/charges use `Decimal`; indicators may use floats. Timestamps are timezone-aware UTC.

## Token discipline
Do not restate task or files. Output only code changes, commands with real output, and report. Paste only the last 3 lines of pytest output, never progress dots. If budget runs low, stop safely and append via `scripts/handoff.sh add "<line>"`.

## Efficiency (saves tokens and time)
- Run commands in the foreground via `scripts/run.sh <command...>`. No background tasks; no polling.
- Before opening files to learn signatures, read `_workbench/API_INDEX.md` (`algodesk api index`). Read files using search plus ranged reads from `API_INDEX.md` line ranges. Open a file only for parts changed; read at most once per checkpoint.
- While iterating, use `bash scripts/t.sh` and `bash scripts/check.sh --quick`. Run full `bash scripts/check.sh` once at card end and after any checkpoint changing engine, oms, risk, portfolio, journal, or fees. Use `--summary` for failing sections only.
- Pick the simplest mechanism satisfying `Accept`; do not experiment with internals.

## Finish
1. Run card `Accept` commands; paste real output.
2. Run `bash scripts/check.sh`; paste the last line.
3. Leave repository committable.
4. Write report in skill `algodesk-evidence` schema.
