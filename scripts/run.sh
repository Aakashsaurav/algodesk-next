#!/usr/bin/env bash
# scripts/run.sh <command...>: foreground with a timeout argument; prints the exit code and the last 25 lines.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/env.sh"

TIMEOUT=300

while [ $# -gt 0 ]; do
    case "$1" in
        --timeout|-t)
            TIMEOUT="$2"
            shift 2
            ;;
        *)
            break
            ;;
    esac
done

if [ $# -eq 0 ]; then
    echo "Usage: scripts/run.sh [--timeout <seconds>] <command...>" >&2
    exit 2
fi

python3 - "$TIMEOUT" "$@" <<'EOF'
import sys, subprocess

timeout = float(sys.argv[1])
cmd = sys.argv[2:]

try:
    proc = subprocess.run(cmd, timeout=timeout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out_lines = proc.stdout.splitlines()
    print(f"EXIT: {proc.returncode}")
    for line in out_lines[-25:]:
        print(line)
    sys.exit(proc.returncode)
except subprocess.TimeoutExpired as exc:
    print(f"TIMEOUT: command exceeded {timeout}s limit (exit 124)")
    if exc.stdout:
        raw = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        for line in raw.splitlines()[-25:]:
            print(line)
    sys.exit(124)
EOF

