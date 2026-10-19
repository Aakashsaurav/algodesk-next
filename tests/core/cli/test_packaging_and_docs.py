"""Tests verifying CLI packaging, import-linter contracts, and documentation."""

from __future__ import annotations

import tomllib

from algodesk.ops.paths import find_project_root


def test_pyproject_scripts_entry() -> None:
    """pyproject.toml defines algodesk = 'algodesk.cli:main'."""
    root = find_project_root()
    with open(root / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    scripts = data.get("project", {}).get("scripts", {})
    assert scripts.get("algodesk") == "algodesk.cli:main"


def test_importlinter_layers_contract() -> None:
    """.importlinter contains architecture-layers contract."""
    root = find_project_root()
    content = (root / ".importlinter").read_text(encoding="utf-8")
    assert "architecture-layers" in content
    assert "algodesk.cli" in content
    assert "algodesk.ops" in content
    assert "algodesk.api" in content


def test_readme_lists_all_commands_and_exit_codes() -> None:
    """README.md contains Summary <= 150 words and lists all commands and exit codes."""
    root = find_project_root()
    content = (root / "README.md").read_text(encoding="utf-8")

    # Check Summary
    lines = content.splitlines()
    assert lines[0] == "## Summary"
    summary_words = []
    for line in lines[1:]:
        if line.startswith("###"):
            break
        summary_words.extend(line.split())
    assert len(summary_words) <= 150

    # Check commands listed
    expected_commands = [
        "algodesk version",
        "algodesk doctor",
        "algodesk config show",
        "algodesk param show",
        "algodesk param set",
        "algodesk param history",
        "algodesk param reset",
        "algodesk plugins list",
        "algodesk plugins check",
        "algodesk plugins test",
        "algodesk new plugin",
        "algodesk journal verify",
        "algodesk journal tail",
        "algodesk journal runs",
    ]
    for cmd in expected_commands:
        assert cmd in content, f"Missing command '{cmd}' in README.md"

    # Check exit codes listed
    for code in ["0", "1", "2", "70"]:
        assert f"`{code}`" in content or f"Exit code {code}" in content or f"- `{code}`" in content
