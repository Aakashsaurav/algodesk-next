---
name: algodesk-evidence
description: Use at the start of any task (to read the task card format) and at the end (to write the report and handoff). Defines evidence rules.
---
# Task card and report

## Summary
Cards are short and mechanical. Reports are short and prove things with real command output. Never claim success without evidence.

## Task card fields
`ID`, `Goal` (one line), `Reqs` (PRD IDs), `Read` (paths), `Touch` (only these paths), `Do` (at most 8 steps), `Accept` (commands with expected result), `Report`. At most 400 words, one deliverable, at most 300 changed lines.

## Report schema (at most 300 words, summary first)
```
SUMMARY: <outcome: complete | partial | blocked> in one or two sentences
CHANGED: <files added / edited / deleted>
EVIDENCE: <each Accept command and its real output, trimmed>
CHECK: <last line of scripts/check.sh>
NOT DONE: <anything skipped and why>
DEVIATIONS: <differences from the card, or "none">
CONFIRM: no live orders; no secrets written; only Touch paths changed
HANDOFF (<=100 words): <state now> | <next step> | <open issues>
```

## Evidence rules
- Paste real output; never paraphrase a result.
- A test that was not run is listed under NOT DONE.
- Do not mark complete if any Accept command fails.
- Partial work must still leave the repository committable.
