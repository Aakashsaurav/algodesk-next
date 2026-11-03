"""Tests for scripts/t.sh test mapping and CLI execution."""

from __future__ import annotations

import subprocess

from algodesk.devtools.test_mapper import map_path_to_tests, resolve_test_targets
from algodesk.ops.paths import find_project_root


def test_map_path_to_tests_core_and_plugins() -> None:
    root = find_project_root()

    # Core source path mapping
    t1 = map_path_to_tests("src/algodesk/engine/runner.py", root=root)
    assert len(t1) > 0
    assert any("tests/core/engine" in t for t in t1)

    t2 = map_path_to_tests("src/algodesk/cli/run_replay.py", root=root)
    assert len(t2) > 0
    assert any("tests/core/cli" in t for t in t2)

    # Plugin path mapping
    t3 = map_path_to_tests("plugins/brokers/upstox/plugin.py", root=root)
    assert t3 == ["plugins/brokers/upstox/tests"]

    # Direct test path mapping
    t4 = map_path_to_tests("tests/core/cli/test_calendar_commands.py", root=root)
    assert t4 == ["tests/core/cli/test_calendar_commands.py"]

    # Fallback / non-mapped file
    t5 = map_path_to_tests("README.md", root=root)
    assert t5 == []


def test_resolve_test_targets_dedup_and_fallback() -> None:
    root = find_project_root()

    # Empty inputs fallback to tests/core
    targets = resolve_test_targets([], root=root)
    assert targets == ["tests/core"]

    # Deduplicated targets
    targets2 = resolve_test_targets(
        ["src/algodesk/cli/data.py", "src/algodesk/cli/session.py"], root=root
    )
    assert targets2 == ["tests/core/cli"]


def test_t_sh_script_execution() -> None:
    root = find_project_root()
    cmd = ["bash", "scripts/t.sh", "tests/core/cli/test_calendar_commands.py"]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "PASS:" in proc.stdout
