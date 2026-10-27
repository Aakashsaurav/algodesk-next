"""Fence-proof tests (CB-1..CB-9, DOC-1..DOC-10, INV-9).

Deliberate violation fixtures make each tool fail.
"""

from __future__ import annotations

from pathlib import Path

from algodesk.devtools.archlint import run_archlint
from algodesk.devtools.budget import run_budget
from algodesk.devtools.docs import run_docs_check, write_index
from algodesk.devtools.secretscan import run_secretscan


def test_fence_clean_repository_passes_all_fences() -> None:
    """The clean repository passes all four quality fence tools."""
    root = Path(".")
    b_rep = run_budget(root)
    assert b_rep.exit_code == 0
    assert b_rep.summary.status in ("OK", "ADVISORY")

    d_rep = run_docs_check(root)
    assert d_rep.exit_code == 0
    assert len(d_rep.errors) == 0

    l_rep = run_archlint(root)
    assert l_rep.exit_code == 0
    assert len(l_rep.errors) == 0

    s_rep = run_secretscan(root)
    assert s_rep.exit_code == 0
    assert len(s_rep.findings) == 0


def test_fence_budget_dependency_violation_fails(tmp_path: Path) -> None:
    """Deliberate dependency overshoot fixture makes budget tool fail."""
    src_dir = tmp_path / "src" / "algodesk" / "mod"
    src_dir.mkdir(parents=True)
    (src_dir / "app.py").write_text("import dep1\nimport dep2\nimport dep3\nimport dep4\nx = 1\n")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['dep1', 'dep2', 'dep3', 'dep4']\n"
    )
    params = {
        "budget.core_lines_guidance": 1000,
        "budget.core_modules_guidance": 10,
        "budget.plugin_lines_guidance": 500,
        "budget.warn_overshoot_pct": 25.0,
        "budget.max_runtime_dependencies": 10,
        "budget.max_core_dependencies": 2,  # 4 > 2 -> FAIL
    }
    rep = run_budget(tmp_path, params)
    assert rep.summary.status == "FAIL"
    assert rep.exit_code == 1


def test_fence_docs_missing_summary_fails(tmp_path: Path) -> None:
    """Deliberate missing summary heading fixture makes docs check fail."""
    (tmp_path / "README.md").write_text("# Project Without Summary Heading\n\nContent.\n")
    rep = run_docs_check(tmp_path)
    assert rep.exit_code == 1
    assert any(e.rule == "docs.summary_heading" for e in rep.errors)


def test_fence_docs_stale_index_fails(tmp_path: Path) -> None:
    """Deliberate stale docs/INDEX.md fixture makes docs check fail."""
    (tmp_path / "README.md").write_text("## Summary\n\nReadme summary.\n")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "STATUS.md").write_text(
        "## Summary\n\nInitial status.\n\n<!-- Owner: core | Version: 1.0 -->\n"
    )
    write_index(tmp_path)

    # Modify STATUS summary without regenerating INDEX.md -> stale!
    (docs_dir / "STATUS.md").write_text(
        "## Summary\n\nModified status.\n\n<!-- Owner: core | Version: 1.0 -->\n"
    )
    rep = run_docs_check(tmp_path)
    assert rep.exit_code == 1
    assert any(e.rule == "docs.index_stale" for e in rep.errors)


def test_fence_docs_broken_link_fails(tmp_path: Path) -> None:
    """Deliberate broken relative link fixture makes docs check fail."""
    (tmp_path / "README.md").write_text(
        "## Summary\n\nReadme summary.\n\nSee [absent guide](docs/nonexistent.md).\n"
    )
    rep = run_docs_check(tmp_path)
    assert rep.exit_code == 1
    assert any(e.rule == "docs.broken_link" for e in rep.errors)


def test_fence_archlint_banned_eval_fails(tmp_path: Path) -> None:
    """Deliberate eval() call fixture makes archlint fail."""
    src = tmp_path / "src" / "algodesk"
    src.mkdir(parents=True)
    (src / "unsafe.py").write_text("def run(s: str) -> None:\n    eval(s)\n")
    rep = run_archlint(tmp_path)
    assert rep.exit_code == 1
    assert any(e.rule == "archlint.banned_call" for e in rep.errors)


def test_fence_archlint_stub_detector_fails(tmp_path: Path) -> None:
    """Deliberate empty stub body fixture makes archlint fail."""
    src = tmp_path / "src" / "algodesk"
    src.mkdir(parents=True)
    (src / "service.py").write_text("def fetch_data():\n    pass\n")
    rep = run_archlint(tmp_path)
    assert rep.exit_code == 1
    assert any(e.rule == "archlint.stub_detector" for e in rep.errors)


def test_fence_archlint_hard_coded_limit_fails(tmp_path: Path) -> None:
    """Deliberate hard-coded limit fixture makes archlint fail."""
    src = tmp_path / "src" / "algodesk"
    src.mkdir(parents=True)
    (src / "handler.py").write_text("timeout_seconds = 45\n")
    rep = run_archlint(tmp_path)
    assert rep.exit_code == 1
    assert any(e.rule == "archlint.hard_coded_limit" for e in rep.errors)


def test_fence_archlint_plugin_import_fails(tmp_path: Path) -> None:
    """Deliberate cross-package plugin import makes archlint fail."""
    p_dir = tmp_path / "plugins" / "notifier" / "testplug"
    p_dir.mkdir(parents=True)
    (p_dir / "plugin.toml").write_text(
        "[plugin]\nname='testplug'\nkind='notifier'\nversion='0.1'\n"
    )
    (p_dir / "plugin.py").write_text("import algodesk.journal\n")
    rep = run_archlint(tmp_path)
    assert rep.exit_code == 1
    assert any(e.rule == "archlint.plugin_import" for e in rep.errors)


def test_fence_archlint_duplicate_class_fails(tmp_path: Path) -> None:
    """Deliberate duplicate top-level class names across modules make archlint fail."""
    src = tmp_path / "src" / "algodesk"
    src.mkdir(parents=True)
    (src / "a.py").write_text("class ConflictingService:\n    val = 1\n")
    (src / "b.py").write_text("class ConflictingService:\n    val = 2\n")
    rep = run_archlint(tmp_path)
    assert rep.exit_code == 1
    assert any(e.rule == "archlint.duplicate_class" for e in rep.errors)


def test_fence_secretscan_private_key_fails(tmp_path: Path) -> None:
    """Deliberate RSA private key block fixture makes secret scan fail."""
    (tmp_path / "id_rsa").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\n"
    )
    rep = run_secretscan(tmp_path)
    assert rep.exit_code == 1
    assert any(f.rule == "secrets.private_key" for f in rep.findings)


def test_fence_secretscan_cloud_key_fails(tmp_path: Path) -> None:
    """Deliberate AWS cloud key fixture makes secret scan fail."""
    (tmp_path / "cloud.env").write_text("KEY=AKIAIOSFODNN7EXAMPLE\n")
    rep = run_secretscan(tmp_path)
    assert rep.exit_code == 1
    assert any(f.rule == "secrets.cloud_access_key" for f in rep.findings)


def test_fence_secretscan_github_token_fails(tmp_path: Path) -> None:
    """Deliberate GitHub personal token fixture makes secret scan fail."""
    (tmp_path / "token.txt").write_text("ghp_123456789012345678901234567890123456\n")
    rep = run_secretscan(tmp_path)
    assert rep.exit_code == 1
    assert any(f.rule == "secrets.github_token" for f in rep.findings)


def test_fence_secretscan_telegram_token_fails(tmp_path: Path) -> None:
    """Deliberate Telegram bot token fixture makes secret scan fail."""
    (tmp_path / "tele.txt").write_text("123456789:abcdefghijklmnopqrstuvwxyz123456789\n")
    rep = run_secretscan(tmp_path)
    assert rep.exit_code == 1
    assert any(f.rule == "secrets.telegram_token" for f in rep.findings)


def test_fence_secretscan_quoted_secret_fails(tmp_path: Path) -> None:
    """Deliberate raw credential assignment fixture makes secret scan fail."""
    (tmp_path / "secrets.toml").write_text("api_key = 'super_high_entropy_secret_token_123'\n")
    rep = run_secretscan(tmp_path)
    assert rep.exit_code == 1
    assert any(f.rule == "secrets.quoted_secret" for f in rep.findings)
