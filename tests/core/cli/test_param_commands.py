"""Tests for algodesk param CLI commands (show, set, history, reset)."""

from __future__ import annotations

import getpass
import json
from typing import Any

from typer.testing import CliRunner

from algodesk.cli.app import app


def test_param_show_table(runner: CliRunner, make_project: Any) -> None:
    """param show displays parameters in an aligned table."""
    proj = make_project("p_show")
    result = runner.invoke(app, ["--root", str(proj), "param", "show"])
    assert result.exit_code == 0
    assert "NAME" in result.output
    assert "ops.min_free_disk_gb" in result.output
    assert "risk.conservative.daily_loss_limit_pct" in result.output


def test_param_show_prefix_filter(runner: CliRunner, make_project: Any) -> None:
    """param show [PREFIX] filters to only matching parameters."""
    proj = make_project("p_filter")
    result = runner.invoke(app, ["--root", str(proj), "param", "show", "risk.conservative"])
    assert result.exit_code == 0
    assert "risk.conservative.daily_loss_limit_pct" in result.output
    assert "ops.min_free_disk_gb" not in result.output


def test_param_show_json(runner: CliRunner, make_project: Any) -> None:
    """param show --json outputs valid JSON array."""
    proj = make_project("p_json")
    result = runner.invoke(app, ["--json", "--root", str(proj), "param", "show"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    names = [row["name"] for row in data]
    assert "ops.min_free_disk_gb" in names


def test_param_set_tighten_flow(runner: CliRunner, make_project: Any) -> None:
    """param set applies tighten change immediately with reason and username."""
    proj = make_project("p_set")
    result = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "1.0",
            "--reason",
            "tighten for volatility",
        ],
    )
    assert result.exit_code == 0
    assert "OK: risk.conservative.daily_loss_limit_pct -> 1.0 (tighten, applied)" in result.output

    # Verify updated value in param show
    show_res = runner.invoke(
        app,
        ["--root", str(proj), "param", "show", "risk.conservative.daily_loss_limit_pct"],
    )
    assert "1.0" in show_res.output


def test_param_set_missing_reason_usage_error(runner: CliRunner, make_project: Any) -> None:
    """param set without --reason is a usage error that exits code 2."""
    proj = make_project("p_no_reason")
    result = runner.invoke(
        app,
        ["--root", str(proj), "param", "set", "risk.conservative.daily_loss_limit_pct", "1.0"],
    )
    assert result.exit_code == 2


def test_param_set_empty_reason_usage_error(runner: CliRunner, make_project: Any) -> None:
    """param set with empty --reason exits code 2."""
    proj = make_project("p_empty_reason")
    result = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "1.0",
            "--reason",
            "",
        ],
    )
    assert result.exit_code == 2


def test_param_set_short_reason_raises_param_error(runner: CliRunner, make_project: Any) -> None:
    """Reason under 5 characters raises ParamError (exit code 1)."""
    proj = make_project("p_short_reason")
    result = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "1.0",
            "--reason",
            "bad",
        ],
    )
    assert result.exit_code == 1
    assert "ERROR: Reason is mandatory and must be at least 5 characters" in result.output


def test_param_set_unknown_parameter(runner: CliRunner, make_project: Any) -> None:
    """Operating on an unknown parameter raises ParamError and exits 1."""
    proj = make_project("p_unknown")
    result = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "nonexistent.param",
            "100",
            "--reason",
            "valid reason here",
        ],
    )
    assert result.exit_code == 1
    assert "ERROR: Cannot operate on unknown parameter 'nonexistent.param'" in result.output


def test_param_set_invalid_type(runner: CliRunner, make_project: Any) -> None:
    """Supplying a string that cannot be parsed as a float raises ParamError (exit 1)."""
    proj = make_project("p_bad_type")
    result = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "not_a_float",
            "--reason",
            "testing invalid float",
        ],
    )
    assert result.exit_code == 1
    assert "ERROR: Cannot parse 'not_a_float' as float" in result.output


def test_param_history_and_filter(runner: CliRunner, make_project: Any) -> None:
    """param history records user name, channel 'cli', and displays records."""
    proj = make_project("p_hist")
    runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "1.2",
            "--reason",
            "tighten margin",
        ],
    )
    hist_res = runner.invoke(app, ["--root", str(proj), "param", "history"])
    assert hist_res.exit_code == 0
    assert "tighten margin" in hist_res.output
    assert getpass.getuser() in hist_res.output

    # Filter by name
    filter_res = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "history",
            "risk.conservative.daily_loss_limit_pct",
            "--limit",
            "5",
        ],
    )
    assert filter_res.exit_code == 0
    assert "tighten margin" in filter_res.output


def test_param_reset_flow(runner: CliRunner, make_project: Any) -> None:
    """param reset restores the default value and logs history."""
    proj = make_project("p_reset")
    runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "1.1",
            "--reason",
            "tighten for test",
        ],
    )

    reset_res = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "reset",
            "risk.conservative.daily_loss_limit_pct",
            "--reason",
            "reverting to default",
        ],
    )
    assert reset_res.exit_code == 0
    assert "OK: reset risk.conservative.daily_loss_limit_pct -> 1.5" in reset_res.output

    # Check that effective value is back to 1.5
    show_res = runner.invoke(
        app,
        ["--root", str(proj), "param", "show", "risk.conservative.daily_loss_limit_pct"],
    )
    assert "1.5" in show_res.output


def test_param_reset_missing_reason_usage_error(runner: CliRunner, make_project: Any) -> None:
    """param reset without --reason exits code 2."""
    proj = make_project("p_reset_no_reason")
    result = runner.invoke(
        app,
        ["--root", str(proj), "param", "reset", "risk.conservative.daily_loss_limit_pct"],
    )
    assert result.exit_code == 2


def test_param_set_loosen_non_live_applies_at_once(runner: CliRunner, make_project: Any) -> None:
    """Loosening in non-live mode applies immediately without requiring confirm."""
    proj = make_project("p_loosen_non_live")
    result = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "2.0",
            "--reason",
            "loosen in non-live mode",
        ],
    )
    assert result.exit_code == 0
    assert "OK: risk.conservative.daily_loss_limit_pct -> 2.0 (loosen, applied)" in result.output

    # Effective value updated immediately
    show_res = runner.invoke(
        app,
        ["--root", str(proj), "param", "show", "risk.conservative.daily_loss_limit_pct"],
    )
    assert "2.0" in show_res.output


def test_param_set_confirm_option_accepted_in_non_live(
    runner: CliRunner, make_project: Any
) -> None:
    """The --confirm option is accepted in non-live mode, and non-live never rejects."""
    proj = make_project("p_confirm_accepted")
    # Even with an arbitrary or matching confirm phrase, non-live applies at once
    result = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "2.2",
            "--reason",
            "loosen with confirm",
            "--confirm",
            "LOOSEN risk.conservative.daily_loss_limit_pct",
        ],
    )
    assert result.exit_code == 0
    assert "OK: risk.conservative.daily_loss_limit_pct -> 2.2 (loosen, applied)" in result.output

    # Wrong confirm phrase is NOT rejected in non-live mode
    result_arbitrary = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "2.4",
            "--reason",
            "loosen with arbitrary confirm",
            "--confirm",
            "ARBITRARY_PHRASE",
        ],
    )
    assert result_arbitrary.exit_code == 0
    assert (
        "OK: risk.conservative.daily_loss_limit_pct -> 2.4 (loosen, applied)"
        in result_arbitrary.output
    )


def test_param_set_live_armed_requires_and_validates_confirm(
    runner: CliRunner, make_project: Any, monkeypatch: Any
) -> None:
    """When registry reports live and armed, wrong phrase rejected; correct phrase stages."""
    import sys

    from algodesk.api.kinds import Mode

    proj = make_project("p_live_armed")
    cli_mod = sys.modules["algodesk.cli.app"]
    orig_open_rt = cli_mod._open_rt

    def armed_live_open_rt(ctx: Any, *, ensure: bool = False) -> Any:
        rt = orig_open_rt(ctx, ensure=ensure)
        if rt.params is not None:
            rt.params._mode = Mode.LIVE
            rt.params._armed = lambda: True
        return rt

    monkeypatch.setattr(cli_mod, "_open_rt", armed_live_open_rt)

    # 1. Missing confirm is rejected
    res_no_confirm = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "2.0",
            "--reason",
            "loosen in armed live",
        ],
    )
    assert res_no_confirm.exit_code == 1
    assert "requires typed confirmation" in res_no_confirm.output

    # 2. Wrong phrase is rejected
    res_wrong_confirm = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "2.0",
            "--reason",
            "loosen in armed live",
            "--confirm",
            "WRONG PHRASE",
        ],
    )
    assert res_wrong_confirm.exit_code == 1
    assert "requires typed confirmation" in res_wrong_confirm.output

    # 3. Correct phrase is accepted and stages as pending
    res_ok = runner.invoke(
        app,
        [
            "--root",
            str(proj),
            "param",
            "set",
            "risk.conservative.daily_loss_limit_pct",
            "2.0",
            "--reason",
            "loosen in armed live",
            "--confirm",
            "LOOSEN risk.conservative.daily_loss_limit_pct",
        ],
    )
    assert res_ok.exit_code == 0
    assert "OK: risk.conservative.daily_loss_limit_pct -> 2.0 (loosen, pending)" in res_ok.output
