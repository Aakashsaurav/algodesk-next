---
name: algodesk-evidence
description: Use at the start of any task (to read the task card format) and at the end (to write the report and handoff). Defines evidence rules.
---
# Task card and report

## Summary
Cards are short and mechanical. Reports are short and prove things with real command output. Never claim success without evidence.

## Task card fields
`ID`, `Goal` (one line), `Reqs` (PRD IDs), `Read` (paths), `Touch` (only these paths), `Do` (as many steps as the module needs, grouped in checkpoints CP1, CP2, …), `Accept` (commands with expected result), `Report`. At most 800 words; one module per card; no fixed line cap.

## Checkpoints
After each checkpoint (CPn) in the card: run `bash scripts/check.sh`, then append one line `CPn done | next step | files` to `_workbench/handoff.md`. If your budget runs low, stop at the last finished checkpoint.

## Report schema (at most 300 words, summary first)
```
SUMMARY: <outcome: complete | partial | blocked> in one or two sentences
CHANGED: <files added / edited / deleted>
EVIDENCE: <each Accept command and its real output, trimmed>
CHECK RESULT: <exact last line printed by scripts/check.sh, verbatim>
NOT DONE: <anything skipped and why>
DEVIATIONS: <differences from the card, or "none">
CONFIRM: no live orders; no secrets written; only Touch paths changed
HANDOFF (<=100 words): <state now> | <next step> | <open issues>
```

## Evidence rules
- Paste real output; never paraphrase a result. Always include the raw `git status --short`.
- A test that was not run is listed under NOT DONE.
- Do not mark complete if any Accept command fails.
- Partial work must still leave the repository committable.
