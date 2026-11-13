"""Tests ensuring graphify artifacts and skills do not break quality gates."""

from __future__ import annotations

from pathlib import Path

from algodesk.devtools.archlint import load_archlint_config, run_archlint
from algodesk.devtools.docs import run_docs_check
from algodesk.devtools.secretscan import load_secretscan_config, run_secretscan


def test_archlint_and_secretscan_config_exclude_graphify_out(tmp_path: Path) -> None:
    """Config files must explicitly exclude graphify-out directory."""
    root = Path(__file__).resolve().parents[3]
    arch_cfg = load_archlint_config(root)
    assert "graphify-out" in arch_cfg.excluded_dirs

    sec_cfg = load_secretscan_config(root)
    assert "graphify-out" in sec_cfg.excluded_dirs


def test_graphify_presence_does_not_break_quality_gates(tmp_path: Path) -> None:
    """Simulate graphify-out and .agents/skills/graphify artifacts; verify gates pass."""
    # Create simulated dummy project structure
    sim_root = tmp_path / "proj"
    sim_root.mkdir()
    (sim_root / "config").mkdir()
    (sim_root / "config" / "archlint.toml").write_text(
        "[exclusions]\ndirectories = ['.agents', '_workbench', '__pycache__', 'graphify-out']\n"
    )
    (sim_root / "config" / "secretscan.toml").write_text(
        "[exclusions]\ndirectories = ['.agents', '_workbench', '__pycache__', 'graphify-out']\n"
    )
    (sim_root / "config" / "secretscan_allow.toml").write_text("")
    (sim_root / "config" / "archlint_allow.toml").write_text("")

    # Populate graphify-out with potential false-positive secret patterns and doc files
    graphify_out = sim_root / "graphify-out"
    graphify_out.mkdir()
    (graphify_out / "GRAPH_REPORT.md").write_text("# Graph Report\n\nNo summary header here.\n")
    (graphify_out / "analysis.json").write_text('{"token": "simulated_secret_token_1234567890"}')

    # Populate .agents/skills/graphify with markdown and code
    skill_dir = sim_root / ".agents" / "skills" / "graphify"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Skill\n\nNo summary.\n")

    # Archlint check
    arch_rep = run_archlint(sim_root)
    assert arch_rep.exit_code == 0

    # Secret scan check
    sec_rep = run_secretscan(sim_root)
    assert sec_rep.exit_code == 0

    # Docs check
    docs_rep = run_docs_check(sim_root)
    assert docs_rep.exit_code == 0
