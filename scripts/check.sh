#!/usr/bin/env bash
# scripts/check.sh — run all quality gates; print CHECK: PASS or CHECK: FAIL (n).
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/env.sh"
export CHECK_SH_RUNNING=1

QUICK=0
SUMMARY=0

for arg in "$@"; do
    case "$arg" in
        --quick)
            QUICK=1
            ;;
        --summary)
            SUMMARY=1
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

failures=0

run_step() {
    local header="$1"
    shift
    if [ "$SUMMARY" -eq 1 ]; then
        local out
        out="$("$@" 2>&1)"
        local status=$?
        if [ $status -ne 0 ]; then
            ((failures++))
            echo "FAIL: $header"
            echo "$out" | tail -n 2
        fi
    else
        echo ""
        echo "=== $header ==="
        "$@" || ((failures++))
    fi
}

# === isolation ===
python_dir="$(uv python dir 2>/dev/null)"
cache_dir="$(uv cache dir 2>/dev/null)"
tool_dir="$(uv tool dir 2>/dev/null)"

if [[ "$python_dir" == "$ROOT"* && "$cache_dir" == "$ROOT"* && "$tool_dir" == "$ROOT"* ]]; then
    if [ "$SUMMARY" -eq 0 ]; then
        echo ""
        echo "=== isolation ==="
        echo "ISOLATION: OK"
    fi
else
    ((failures++))
    if [ "$SUMMARY" -eq 1 ]; then
        echo "FAIL: isolation"
        echo "  expected under: $ROOT"
    else
        echo ""
        echo "=== isolation ==="
        echo "ISOLATION: FAIL"
        echo "  python dir: $python_dir"
        echo "  cache dir:  $cache_dir"
        echo "  tool dir:   $tool_dir"
        echo "  expected under: $ROOT"
    fi
fi

# === ruff check ===
run_step "ruff check" uv run ruff check src tests

if [ "$QUICK" -eq 0 ]; then
    # === ruff format --check ===
    run_step "ruff format --check" uv run ruff format --check src tests
fi

# === mypy ===
run_step "mypy" uv run mypy src

# === pytest ===
if [ "$QUICK" -eq 1 ]; then
    run_step "pytest" uv run pytest -x -q -n auto
else
    run_step "pytest" uv run pytest -q
fi

if [ "$QUICK" -eq 0 ]; then
    # === lint-imports ===
    run_step "lint-imports" uv run lint-imports

    # === budget ===
    run_step "budget" uv run algodesk budget

    # === docs check ===
    run_step "docs check" uv run algodesk docs check

    # === lint ===
    run_step "lint" uv run algodesk lint

    # === secrets scan ===
    run_step "secrets scan" uv run algodesk secrets scan
fi

echo ""
if [ "$failures" -eq 0 ]; then
    echo "CHECK: PASS"
    exit 0
else
    echo "CHECK: FAIL ($failures)"
    exit 1
fi

