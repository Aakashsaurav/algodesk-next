---
name: algodesk-implement
description: Use when implementing or changing AlgoDesk Next code from a task card: modules, tests, wiring into the core or a plugin.
---
# Implement a task card

## Summary
Tests first, smallest change, only the allowed paths, real evidence at the end.

## Steps
1. Read the card. List the files you will change; every one must be inside `Touch`.
2. Search the repository for existing code that already does this; reuse it.
3. Write the failing test first (include one failure-case test).
4. Implement the smallest change that passes. Prefer pure functions; no global state.
5. Run the focused tests, then `bash scripts/check.sh`.
6. Stop. Do not polish, rename or refactor outside `Touch`.
7. Write the report (skill `algodesk-evidence`).

## Conventions
- Python 3.13, full type hints, `pydantic` for external data, `dataclasses` for internal records.
- Errors: subclass `AlgoDeskError`; message = what, where, what to do next.
- No limits or thresholds in code: use the parameter registry.
- Never log or print secrets. Never call the network in tests; use fakes.
- One implementation per concern; if a second appears, delete one.
- Keep code short, but never at the cost of quality, documentation or speed. Size numbers are guidance, not limits.

## Stop and ask if
A requirement conflicts, an API is uncertain, a dependency is needed, or the change would exceed about 300 lines.
