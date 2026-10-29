"""Tests for SessionRunner component wiring, prestart doctor checks, and dry-run."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from algodesk.api import SafetyViolation
from algodesk.api.checks import CheckResult, DoctorCheck, Severity
from algodesk.engine.clock import SimClock
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime


def test_runner_initialization_and_prestart_doctor(tmp_project: Path) -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    with open_runtime(tmp_project, run="demo_paper") as runtime:
        runner = SessionRunner(
            runtime=runtime,
            mode="paper",
            dry_run=True,
            clock=clock,
        )
        assert runner.run_name == "demo_paper"
        assert runner.mode.value == "paper"
        assert "demo_paper_paper_" in runner.run_id
        # Prestart doctor should pass cleanly for demo_paper
        runner.run_prestart_doctor()


def test_runner_prestart_doctor_refusal(tmp_project: Path) -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    with open_runtime(tmp_project, run="demo_paper") as runtime:
        runner = SessionRunner(
            runtime=runtime,
            mode="paper",
            dry_run=True,
            clock=clock,
        )

        critical_check = DoctorCheck(
            id="test.failing_check",
            severity=Severity.CRITICAL,
            run=lambda: CheckResult(
                ok=False,
                message="Critical safety failure",
                fix="Resolve the issue",
            ),
        )

        with patch("algodesk.engine.runner.build_run_checks", return_value=[critical_check]):
            with pytest.raises(SafetyViolation) as exc_info:
                runner.run_prestart_doctor()
            assert "Pre-start doctor check failed with 1 critical error" in str(exc_info.value)


def test_runner_dry_run_skips_execution(tmp_project: Path) -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    with open_runtime(tmp_project, run="demo_paper") as runtime:
        runner = SessionRunner(
            runtime=runtime,
            mode="paper",
            dry_run=True,
            clock=clock,
        )
        res_path = asyncio.run(runner.run())
        assert res_path.name == runner.run_id


def test_runner_observe_mode_wires_risk_engine(tmp_project: Path) -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    with open_runtime(tmp_project, run="demo_paper") as runtime:
        runner = SessionRunner(
            runtime=runtime,
            mode="paper",
            observe=True,
            dry_run=False,
            clock=clock,
        )
        assert runner.observe is True
        journal = runner._init_journal()
        components = runner._build_components(clock, journal)
        risk_engine = components["risk_engine"]
        assert risk_engine.observe is True


def test_runner_stop_sets_event(tmp_project: Path) -> None:
    clock = SimClock(datetime(2026, 10, 26, 9, 15, tzinfo=UTC))
    with open_runtime(tmp_project, run="demo_paper") as runtime:
        runner = SessionRunner(runtime=runtime, clock=clock)
        assert not runner._stop_event.is_set()
        runner.stop()
        assert runner._stop_event.is_set()
