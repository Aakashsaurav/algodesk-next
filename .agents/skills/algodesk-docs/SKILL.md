---
name: algodesk-docs
description: Use when writing or editing any documentation, README, decision record, runbook or report in AlgoDesk Next.
---
# Documentation rules

## Summary
Few documents, each starting with a Summary that tells the whole story in at most 150 words. One home per fact. Docs change in the same commit as the code.

## Rules
1. Every Markdown file starts with `## Summary` (at most 150 words) and metadata (owner, version, date).
2. At most 12 living documents in `docs/`. Do not add a new one; extend an existing one or ask.
3. Plugin README: at most one page; Summary first; `verified_on` facts with sources.
4. Decision record: at most 300 words (context, decision, why, consequences).
5. Runbook: checklists, not essays.
6. Link, do not copy. Delete stale text rather than archiving it in the repo.
7. Planning material (chats, drafts, prompts) goes in `_workbench/`, never `docs/`.
8. Run `algodesk docs index` then `algodesk docs check` before finishing.
9. Module design docs (5 or more pages) are condensed into a one-page module README after the module is built; the long form moves to `_workbench/`.

## Style
Plain language for a beginner; short sentences; tables for comparisons; no filler.
