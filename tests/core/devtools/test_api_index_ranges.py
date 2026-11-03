"""Tests for AST API index file:start-end line ranges."""

from __future__ import annotations

import re

from algodesk.devtools.api_index import generate_api_index
from algodesk.ops.paths import find_project_root


def test_api_index_contains_valid_line_ranges() -> None:
    root = find_project_root()
    content = generate_api_index(root)

    # Match lines like: - src/algodesk/api/errors.py:10-30: `class AlgoDeskError(Exception)`
    entry_re = re.compile(r"^- ([a-zA-Z0-9_\/.]+):(\d+)-(\d+): `(class |async def |def )?([^`]+)`")

    class_or_func_count = 0
    for line in content.splitlines():
        if "`class " in line or "(" in line:
            m = entry_re.match(line)
            if m:
                rel_path, start_str, end_str, _, name = m.groups()
                start = int(start_str)
                end = int(end_str)
                assert start > 0, f"Invalid start line in {line}"
                assert start <= end, f"Start line exceeds end line in {line}"
                file_path = root / rel_path
                assert file_path.is_file(), f"Referenced file not found: {rel_path}"
                class_or_func_count += 1

    assert class_or_func_count > 100, f"Expected >100 indexed symbols, found {class_or_func_count}"
