#!/usr/bin/env bash
# scripts/check.sh — run all quality gates; print CHECK: PASS or CHECK: FAIL (n).
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/env.sh"
export CHECK_SH_RUNNING=1

failures=0

echo ""
echo "=== isolation ==="
python_dir="$(uv python dir 2>/dev/null)"
cache_dir="$(uv cache dir 2>/dev/null)"
tool_dir="$(uv tool dir 2>/dev/null)"

if [[ "$python_dir" == "$ROOT"* && "$cache_dir" == "$ROOT"* && "$tool_dir" == "$ROOT"* ]]; then
    echo "ISOLATION: OK"
else
    echo "ISOLATION: FAIL"
    echo "  python dir: $python_dir"
    echo "  cache dir:  $cache_dir"
    echo "  tool dir:   $tool_dir"
    echo "  expected under: $ROOT"
    ((failures++))
fi

echo ""
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
echo "=== budget ==="
uv run algodesk budget || ((failures++))

echo ""
echo "=== docs check ==="
uv run algodesk docs check || ((failures++))

echo ""
echo "=== lint ==="
uv run algodesk lint || ((failures++))

echo ""
echo "=== secrets scan ==="
uv run algodesk secrets scan || ((failures++))

echo ""
if [ "$failures" -eq 0 ]; then
    echo "CHECK: PASS"
    exit 0
else
    echo "CHECK: FAIL ($failures)"
    exit 1
fi
