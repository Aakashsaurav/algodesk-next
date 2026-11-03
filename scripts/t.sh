#!/usr/bin/env bash
# scripts/t.sh — fast test loop runner mapping diffs to targeted tests.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/env.sh"

TARGETS="$(uv run python -m algodesk.devtools.test_mapper "$@")"

# Run pytest on targets
OUT="$(uv run pytest -x -q --tb=short -n auto $TARGETS 2>&1)"
STATUS=$?

if [ $STATUS -eq 0 ]; then
    SUMMARY_LINE="$(echo "$OUT" | grep -E "passed|warnings" | tail -n 1)"
    if [ -z "$SUMMARY_LINE" ]; then
        SUMMARY_LINE="$(echo "$OUT" | tail -n 1)"
    fi
    echo "PASS: $SUMMARY_LINE"
    exit 0
else
    echo "FAIL: tests failed (exit $STATUS)"
    echo "$OUT" | tail -n 15
    exit $STATUS
fi

