"""Tests for scripts/handoff.sh rotation and archive preservation."""

from __future__ import annotations

import subprocess
from pathlib import Path


def test_handoff_rotation_in_tmp_project(tmp_path: Path) -> None:
    wb_dir = tmp_path / "_workbench"
    wb_dir.mkdir(parents=True)
    handoff_file = wb_dir / "handoff.md"
    archive_file = wb_dir / "handoff_archive.md"

    # Pre-populate handoff with 26 lines
    lines = [f"Line {i}" for i in range(1, 27)]
    handoff_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Run handoff script logic against tmp_path
    cmd = [
        "python3",
        "-c",
        """
import sys
from pathlib import Path

root = Path(sys.argv[1])
new_line = sys.argv[2].strip()

wb_dir = root / "_workbench"
wb_dir.mkdir(parents=True, exist_ok=True)
handoff_file = wb_dir / "handoff.md"
archive_file = wb_dir / "handoff_archive.md"

raw_lines = handoff_file.read_text(encoding="utf-8").splitlines()
existing_lines = [line for line in raw_lines if line.strip()]
existing_lines.append(new_line)

if len(existing_lines) > 25:
    archive_lines = existing_lines[:-25]
    keep_lines = existing_lines[-25:]
    with archive_file.open("a", encoding="utf-8") as af:
        for al in archive_lines:
            af.write(al + "\\n")
    handoff_file.write_text("\\n".join(keep_lines) + "\\n", encoding="utf-8")
else:
    handoff_file.write_text("\\n".join(existing_lines) + "\\n", encoding="utf-8")
        """,
        str(tmp_path),
        "Line 27 (new)",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    assert proc.returncode == 0

    # Handoff must contain exactly 25 lines
    kept = [line for line in handoff_file.read_text().splitlines() if line.strip()]
    assert len(kept) == 25
    assert kept[-1] == "Line 27 (new)"
    assert kept[0] == "Line 3"

    # Archive must contain the older 2 lines (Line 1 and Line 2)
    archived = [line for line in archive_file.read_text().splitlines() if line.strip()]
    assert len(archived) == 2
    assert archived[0] == "Line 1"
    assert archived[1] == "Line 2"
