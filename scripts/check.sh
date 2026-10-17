#!/usr/bin/env bash
# scripts/check.sh — run all quality gates; print CHECK: PASS or CHECK: FAIL (n).
set -u

failures=0

echo "=== ruff check ==="
uv run ruff check src tests || ((failures++))

echo ""
echo "=== ruff format --check ==="
uv run ruff format --check src tests || ((failures++))

echo ""
echo "=== mypy ==="
uv run mypy src || ((failures++))

echo ""
echo "=== pytest ==="
uv run pytest -q || ((failures++))

echo ""
echo "=== lint-imports ==="
uv run lint-imports || ((failures++))

echo ""
if [ "$failures" -eq 0 ]; then
    echo "CHECK: PASS"
    exit 0
else
    echo "CHECK: FAIL ($failures)"
    exit 1
fi
