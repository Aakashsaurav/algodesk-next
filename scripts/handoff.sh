#!/usr/bin/env bash
# scripts/handoff.sh — append checkpoint status to handoff.md and rotate older lines to archive.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/env.sh"

ACTION="${1:-}"
if [ "$ACTION" != "add" ] || [ $# -lt 2 ]; then
    echo "Usage: scripts/handoff.sh add \"<CPn done | next | files>\"" >&2
    exit 2
fi

LINE="$2"
python3 - "$ROOT" "$LINE" <<'EOF'
import sys
from pathlib import Path

root = Path(sys.argv[1])
new_line = sys.argv[2].strip()

wb_dir = root / "_workbench"
wb_dir.mkdir(parents=True, exist_ok=True)
handoff_file = wb_dir / "handoff.md"
archive_file = wb_dir / "handoff_archive.md"

existing_lines: list[str] = []
if handoff_file.is_file():
    existing_lines = [
        line for line in handoff_file.read_text(encoding="utf-8").splitlines() if line.strip()
    ]

existing_lines.append(new_line)

if len(existing_lines) > 25:
    archive_lines = existing_lines[:-25]
    keep_lines = existing_lines[-25:]

    # Append to archive
    with archive_file.open("a", encoding="utf-8") as af:
        for al in archive_lines:
            af.write(al + "\n")

    # Rewrite handoff with only the last 25 lines
    handoff_file.write_text("\n".join(keep_lines) + "\n", encoding="utf-8")
    print(f"HANDOFF: added, archived {len(archive_lines)} older lines, kept 25")
else:
    handoff_file.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")
    print(f"HANDOFF: added, total {len(existing_lines)} lines")
EOF

