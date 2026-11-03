"""Tests for plugin conformance skipping and force execution."""

from __future__ import annotations

from typer.testing import CliRunner

from algodesk.cli.app import app


def test_conformance_skip_and_force(runner: CliRunner) -> None:
    # Run once with --run demo_paper_fast
    res1 = runner.invoke(app, ["plugins", "test", "--all", "--run", "demo_paper_fast"])
    assert res1.exit_code == 0
    # At least some should be skipped or passed
    assert "skipped (current)" in res1.output or "passed conformance suite" in res1.output

    # Second run without --force must skip all current plugins
    res2 = runner.invoke(app, ["plugins", "test", "--all", "--run", "demo_paper_fast"])
    assert res2.exit_code == 0
    assert "skipped (current)" in res2.output

    # Third run with --force must re-run all plugins
    res3 = runner.invoke(app, ["plugins", "test", "--all", "--run", "demo_paper_fast", "--force"])
    assert res3.exit_code == 0
    assert "passed conformance suite" in res3.output
