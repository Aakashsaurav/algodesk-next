"""Tests for M4-7 documentation closeout, CLI command validity, and limits."""

from __future__ import annotations

import re
from pathlib import Path

import click
import typer.main

from algodesk.cli.app import app
from algodesk.devtools.docs import extract_summary

ROOT = Path(__file__).resolve().parents[3]


def _collect_cli_paths(cmd: click.Command, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Collect all registered command and group paths from the Click/Typer tree."""
    paths: set[tuple[str, ...]] = set()
    if prefix:
        paths.add(prefix)
    if hasattr(cmd, "list_commands") and hasattr(cmd, "get_command"):
        ctx = click.Context(cmd)
        for name in cmd.list_commands(ctx):
            sub = cmd.get_command(ctx, name)
            if sub is not None:
                paths |= _collect_cli_paths(sub, (*prefix, name))
    return paths


def _extract_runbook_commands(text: str) -> list[list[str]]:
    """Extract literal `algodesk ...` command token lists from RUNBOOK.md."""
    commands: list[list[str]] = []
    # Match `algodesk <tokens>` inside backticks or code blocks
    for line in text.splitlines():
        for match in re.finditer(r"\balgodesk\s+([a-z0-9_-]+(?:\s+[a-z0-9_-]+)*)", line):
            tokens = match.group(1).split()
            if tokens:
                commands.append(tokens)
    return commands


def test_runbook_cli_commands_exist() -> None:
    """Every `algodesk ...` command in RUNBOOK.md resolves in the Typer CLI tree."""
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    root_cmd = typer.main.get_command(app)
    valid_paths = _collect_cli_paths(root_cmd)
    cmds = _extract_runbook_commands(runbook)
    assert cmds, "Expected algodesk commands in RUNBOOK.md"

    for tokens in cmds:
        # Find longest matching command prefix in the CLI tree
        matched = False
        for length in range(min(3, len(tokens)), 0, -1):
            if tuple(tokens[:length]) in valid_paths:
                matched = True
                break
        assert matched, f"Unknown CLI command in RUNBOOK.md: algodesk {' '.join(tokens)}"


def test_runbook_cli_commands_failing_case_detects_bogus() -> None:
    """Failing-case check: bogus command is not in the CLI tree."""
    root_cmd = typer.main.get_command(app)
    valid_paths = _collect_cli_paths(root_cmd)
    assert ("nonexistent_cmd",) not in valid_paths


def test_prd_and_status_milestones_consistent() -> None:
    """PRD §12 milestone table and STATUS.md agree that M0-M4 are Done."""
    prd = (ROOT / "docs" / "PRD.md").read_text(encoding="utf-8")
    status = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    for m in ("M0", "M1", "M2", "M3", "M4"):
        assert re.search(rf"\|\s*{m}\s*\|.*\|\s*Done\s*\|", prd), f"{m} not Done in PRD.md"
        assert re.search(rf"\|\s*{m}\s*\|.*\|\s*Done\s*\|", status), f"{m} not Done in STATUS.md"
    assert "### M4 Requirement Status" in prd
    for req in ("FR-BKT-1", "FR-BKT-10", "FR-RPT-1", "FR-RPT-6", "FR-SCR-6", "FR-FEE-2", "FR-OPS"):
        assert req in prd, f"Missing {req} in PRD M4 Requirement Status"


def test_no_not_built_yet_for_built_items() -> None:
    """ARCHITECTURE.md and STATUS.md must not claim M4 is unbuilt or in progress."""
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    status = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    assert "Not built yet" not in arch
    assert "M4 in progress" not in status
    assert "M0–M4 are complete" in arch or "M0-M4 are complete" in arch


def test_doc_word_and_line_limits() -> None:
    """Verify summary word limits, STATUS word limit, RUNBOOK lines, and ADR-101 length."""
    runbook_text = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert len(runbook_text.splitlines()) <= 450

    status_text = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    assert len(status_text.split()) <= 400

    arch_text = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    m = re.search(r"## 12\. Backtest mode\n(.*?)(?=\n## |\Z)", arch_text, re.DOTALL)
    assert m is not None, "Missing 'Backtest mode' section in ARCHITECTURE.md"
    assert len(m.group(1).strip().splitlines()) <= 60

    decisions_text = (ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    assert "ADR-072 not used (numbering gap)" in decisions_text
    m_adr = re.search(r"\*\*ADR-101 M4 close\.\*\*(.*)", decisions_text, re.DOTALL)
    assert m_adr is not None, "Missing ADR-101 in DECISIONS.md"
    assert len(m_adr.group(1).split()) <= 120

    for md in list((ROOT / "docs").glob("*.md")) + list((ROOT / "plugins").glob("*/*/README.md")):
        _, summary = extract_summary(md.read_text(encoding="utf-8"))
        assert summary, f"Missing summary in {md}"
        assert len(summary.split()) <= 150, f"Summary > 150 words in {md}"


def test_known_limits_and_agents_lessons_present() -> None:
    """Known limits appear in STATUS and RUNBOOK; AGENTS.md has M3/M4 lessons."""
    status = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    for text in (status, runbook):
        assert "## Known Limits" in text
        assert "Survivorship" in text
        assert "Dividends not credited" in text
        assert "Paper fills optimistic" in text

    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Lessons from M3 and M4" in agents
    assert "data/state/killswitch.json" in agents
    assert "POLLS" in agents
