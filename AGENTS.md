# AGENTS.md — rules for every AlgoDesk Next task

## Summary
You are an implementation agent on AlgoDesk Next, a pluggable trading workbench for Indian markets. Do exactly what the task card says, only in the paths it allows, with tests as proof. Stop and ask when unsure. Never place live orders, never touch secrets, never invent broker APIs. Finish every task in a committable state and end with the report and a handoff block.

## Before you start
1. Read the task card, then only the files its `Read` line lists.
2. Inspect existing code before writing. Reuse; one implementation per concern.
3. If a requirement is unclear, conflicting, or needs a new dependency: STOP and ask.

## Hard rules
- Touch only paths in `Touch`. No refactors elsewhere.
- `src/algodesk` never imports from `plugins/`. Plugins never import each other. Strategies never import venues or brokers.
- Orders go only through the order manager. No live orders in any task.
- No stubs that return success. Unsupported means raise `NotSupported`.
- No hard-coded limits, thresholds or timeouts: read them from the parameter registry.
- No secrets in code, config, logs, tests or chat. Use `secret:<name>` references.
- Broker and API facts come only from the official docs the card cites. Never guess an endpoint or field.
- No new dependency unless the card allows it.
- Never weaken or delete a test to make it pass.
- Every Markdown file starts with a Summary of at most 150 words.

## Quality bar
Typed (mypy), linted (ruff), tested (pytest, including a failing-case test), functions at most 50 lines, error messages that say what, where and what to do next. Money and charges use `Decimal`; indicators may use floats. Timestamps are timezone-aware UTC.

## Token discipline
Do not restate the task or the files. Output only code changes, commands with their real output, and the report. If your budget is running low, stop at a safe point and write the handoff block.

## Finish
1. Run the card's `Accept` commands and paste the real output.
2. Run `bash scripts/check.sh` and paste the last line.
3. Leave the repository committable, even if the task is partial.
4. Write the report in the schema of skill `algodesk-evidence`.
