"""Tests for M1-20 CP2: ops.power doctor check, caffeinate keep-awake, and SuspendWatcher."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from algodesk.api.checks import Severity
from algodesk.api.kinds import Mode
from algodesk.cli.app import app
from algodesk.engine.runner import SessionRunner
from algodesk.engine.suspend_watch import SuspendEvent, SuspendWatcher
from algodesk.ops.checks import build_core_checks, build_run_checks
from algodesk.ops.power import check_power, evaluate_power_check, start_keep_awake
from algodesk.ops.runtime import open_runtime

BATT_AC_FIXTURE = """\
Now drawing from 'AC Power'
 -InternalBattery-0 (id=1234567)\t100%; charged; 0:00 remaining present: true
"""

BATT_95_FIXTURE = """\
Now drawing from 'Battery Power'
 -InternalBattery-0 (id=1234567)\t95%; discharging; 5:12 remaining present: true
"""

BATT_35_FIXTURE = """\
Now drawing from 'Battery Power'
 -InternalBattery-0 (id=1234567)\t35%; discharging; 1:45 remaining present: true
"""

ASSERT_ACTIVE_FIXTURE = """\
Assertion status system-wide:
   BackgroundTask                 0
   ApplePushServiceTask           0
   UserIsActive                   1
   PreventUserIdleDisplaySleep    0
   PreventUserIdleSystemSleep     1
   ExternalMedia                  0
Listed by owning process:
   pid 4321(caffeinate): [0x0123] 00:05:00 PreventUserIdleSystemSleep named: "caffeinate"
"""

ASSERT_NONE_FIXTURE = """\
Assertion status system-wide:
   BackgroundTask                 0
   ApplePushServiceTask           0
   UserIsActive                   1
   PreventUserIdleDisplaySleep    0
   PreventUserIdleSystemSleep     0
   ExternalMedia                  0
"""


def test_ops_params_registered(tmp_project: Path) -> None:
    """config/params/ops.toml registers power and suspend detection parameters."""
    with open_runtime(tmp_project, ensure=True) as rt:
        assert rt.params is not None
        assert rt.params.get("ops.keep_awake") is True
        assert rt.params.get("ops.min_battery_pct") == 50
        assert rt.params.get("ops.require_ac") is False
        assert rt.params.get("engine.suspend_detect_seconds") == 30


def test_power_check_non_macos_skips(tmp_project: Path) -> None:
    """Non-macOS platforms skip ops.power check with an informational message."""
    with open_runtime(tmp_project) as rt:
        res = check_power(rt, platform="linux")
        assert res.ok is True
        assert "macOS only" in res.message


def test_power_check_ac_with_assertion_passes() -> None:
    """AC power with active PreventUserIdleSystemSleep assertion passes."""
    res = evaluate_power_check(
        "darwin",
        BATT_AC_FIXTURE,
        ASSERT_ACTIVE_FIXTURE,
        min_battery_pct=50,
        require_ac=False,
        keep_awake=False,
    )
    assert res.ok is True
    assert "AC Power" in res.message


def test_power_check_ac_no_assertion_keep_awake_false_warns() -> None:
    """AC power with no sleep assertion and ops.keep_awake=false warns with caffeinate fix."""
    res = evaluate_power_check(
        "darwin",
        BATT_AC_FIXTURE,
        ASSERT_NONE_FIXTURE,
        min_battery_pct=50,
        require_ac=False,
        keep_awake=False,
    )
    assert res.ok is False
    assert res.severity == Severity.WARNING
    assert "caffeinate -i" in res.fix


def test_power_check_battery_above_min_warns() -> None:
    """Battery above min_battery_pct warns with 'on battery (NN%)' and charger fix."""
    res = evaluate_power_check(
        "darwin",
        BATT_95_FIXTURE,
        ASSERT_ACTIVE_FIXTURE,
        min_battery_pct=50,
        require_ac=False,
        keep_awake=True,
    )
    assert res.ok is False
    assert res.severity == Severity.WARNING
    assert "on battery (95%)" in res.message
    assert "plug in the charger" in res.fix


def test_power_check_battery_below_min_critical() -> None:
    """Battery below min_battery_pct returns CRITICAL with charger fix."""
    res = evaluate_power_check(
        "darwin",
        BATT_35_FIXTURE,
        ASSERT_ACTIVE_FIXTURE,
        min_battery_pct=50,
        require_ac=False,
        keep_awake=True,
    )
    assert res.ok is False
    assert res.severity == Severity.CRITICAL
    assert "35%" in res.message
    assert "plug in the charger" in res.fix


def test_power_check_require_ac_critical() -> None:
    """Battery with ops.require_ac=true returns CRITICAL even at 95%."""
    res = evaluate_power_check(
        "darwin",
        BATT_95_FIXTURE,
        ASSERT_ACTIVE_FIXTURE,
        min_battery_pct=50,
        require_ac=True,
        keep_awake=True,
    )
    assert res.ok is False
    assert res.severity == Severity.CRITICAL
    assert "plug in the charger" in res.fix


def test_power_check_runs_pmset_no_shell_with_timeout(tmp_project: Path) -> None:
    """check_power invokes pmset with shell=False and timeout, and is wired in doctor."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append((cmd, kwargs))
        out = BATT_AC_FIXTURE if "batt" in cmd else ASSERT_ACTIVE_FIXTURE
        return MagicMock(stdout=out, returncode=0)

    with open_runtime(tmp_project) as rt:
        res = check_power(rt, run_cmd=fake_run, platform="darwin")
        assert res.ok is True
        assert len(calls) == 2
        assert calls[0][0] == ["pmset", "-g", "batt"]
        assert calls[1][0] == ["pmset", "-g", "assertions"]
        for _, kw in calls:
            assert kw.get("shell") is False
            assert kw.get("timeout", 0) > 0

        assert any(c.id == "ops.power" for c in build_core_checks(rt))
        assert any(c.id == "ops.power" for c in build_run_checks(rt, "demo_paper"))


def test_start_keep_awake_spawns_caffeinate_and_handles_failure() -> None:
    """start_keep_awake spawns caffeinate -i -w <pid> and returns error string on failure."""
    seen: list[list[str]] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> Any:
        seen.append(cmd)
        return MagicMock(pid=9999)

    proc, err = start_keep_awake(1234, popen_fn=fake_popen, platform="darwin")
    assert err is None
    assert proc is not None and proc.pid == 9999
    assert seen == [["caffeinate", "-i", "-w", "1234"]]

    def failing_popen(cmd: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError("caffeinate not found")

    proc2, err2 = start_keep_awake(1234, popen_fn=failing_popen, platform="darwin")
    assert proc2 is None
    assert err2 is not None and "caffeinate not found" in err2


def test_suspend_watcher_detects_jump_with_fake_clocks() -> None:
    """SuspendWatcher detects wall-vs-monotonic jump > threshold and fires callback."""
    t0 = datetime(2026, 11, 23, 8, 16, 56, tzinfo=UTC)
    wall = [t0]
    mono = [100.0]
    events: list[SuspendEvent] = []

    watcher = SuspendWatcher(
        wall_fn=lambda: wall[0],
        mono_fn=lambda: mono[0],
        threshold_seconds=30.0,
        on_suspend=events.append,
    )

    # Normal 1s tick
    wall[0] = t0 + timedelta(seconds=1)
    mono[0] = 101.0
    assert watcher.check_once() is None

    # Mac slept 556.6s while monotonic only advanced 1.0s (jump = 555.6s)
    wall[0] = wall[0] + timedelta(seconds=556.6)
    mono[0] = 102.0
    ev = watcher.check_once()
    assert ev is not None
    assert ev.seconds == pytest.approx(555.6, abs=0.01)
    assert len(events) == 1
    assert watcher.suspended_events == 1
    assert watcher.suspended_seconds == pytest.approx(555.6, abs=0.01)


def test_session_runner_replay_and_backtest_never_start_keep_awake_or_suspend_watch(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay and backtest never start caffeinate or SuspendWatcher."""
    called: list[str] = []

    def fake_ka(*args: Any, **kwargs: Any) -> tuple[None, None]:
        called.append("keep_awake")
        return None, None

    monkeypatch.setattr("algodesk.ops.power.start_keep_awake", fake_ka)

    with open_runtime(tmp_project, ensure=True) as rt:
        for mode_arg in ("replay", Mode.BACKTEST):
            runner = SessionRunner(rt, mode=mode_arg, replay_dates=["2025-01-02"])
            monkeypatch.setattr(runner, "run_prestart_doctor", lambda: None)
            monkeypatch.setattr(runner, "_build_components", lambda clock, journal: {})

            async def fake_exec(c: Any, clock: Any, journal: Any) -> None:
                return None

            monkeypatch.setattr(runner, "_execute_session", fake_exec)
            res_dir = asyncio.run(runner.run())
            assert runner._suspend_watcher is None
            assert (res_dir / "metrics.json").is_file()

    assert called == []


def test_session_runner_paper_starts_keep_awake_and_journals_suspend(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paper mode starts caffeinate, journals system.keep_awake and system.suspended, and alerts."""
    fake_proc = MagicMock(pid=4242)
    fake_proc.poll.return_value = None
    stopped: list[Any] = []

    monkeypatch.setattr("algodesk.ops.power.start_keep_awake", lambda pid: (fake_proc, None))
    monkeypatch.setattr("algodesk.ops.power.stop_keep_awake", lambda p: stopped.append(p))

    with open_runtime(tmp_project, ensure=True) as rt:
        runner = SessionRunner(rt, mode=Mode.PAPER)
        monkeypatch.setattr(runner, "run_prestart_doctor", lambda: None)
        monkeypatch.setattr(runner, "_build_components", lambda clock, journal: {})

        async def fake_exec(c: Any, clock: Any, journal: Any) -> None:
            from algodesk.engine.suspend_watch import SuspendWatcher

            t0 = datetime(2026, 11, 25, 9, 15, 0, tzinfo=UTC)
            wall = [t0]
            mono = [10.0]
            alerts: list[tuple[str, str]] = []

            def on_susp(ev: SuspendEvent) -> None:
                journal.append(
                    "system.suspended",
                    {"seconds": ev.seconds, "wall_start": ev.wall_start, "wall_end": ev.wall_end},
                )
                alerts.append((ev.wall_start, ev.wall_end))

            watcher = SuspendWatcher(
                wall_fn=lambda: wall[0],
                mono_fn=lambda: mono[0],
                threshold_seconds=30.0,
                on_suspend=on_susp,
            )
            runner._suspend_watcher = watcher
            wall[0] = t0 + timedelta(seconds=120.0)
            mono[0] = 11.0
            watcher.check_once()
            assert len(alerts) == 1

        monkeypatch.setattr(runner, "_execute_session", fake_exec)
        res_dir = asyncio.run(runner.run())

    assert stopped == [fake_proc]
    import json

    metrics = json.loads((res_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["suspended_events"] == 1
    assert metrics["suspended_seconds"] == pytest.approx(119.0, abs=0.1)
    events_lines = (res_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "system.keep_awake" in events_lines
    assert "system.suspended" in events_lines


def test_session_report_prints_suspended_line(tmp_project: Path) -> None:
    """session report prints 'Suspended:' line from metrics.json."""
    run_dir = tmp_project / "results" / "demo_paper_fast_paper_20261123_070703_2a405b"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(
        '{"mode": "paper", "suspended_events": 1, "suspended_seconds": 556.6, "trades": 0}',
        encoding="utf-8",
    )
    (run_dir / "session.md").write_text("Verdict: PASS\nStop Reason: end\n", encoding="utf-8")

    cli = CliRunner()
    res = cli.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "session",
            "report",
            "demo_paper_fast_paper_20261123_070703_2a405b",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "Suspended:" in res.output
    assert "1 event(s), 556.6s" in res.output
