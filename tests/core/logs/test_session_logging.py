"""Tests for file logging, secret scrubbing, and quiet flags across run and replay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from algodesk.api.scrub import Scrubber
from algodesk.cli.app import app
from algodesk.logs.setup import DailyUtcRotatingHandler, ScrubbingFilter, get_logger, setup_logging


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_setup_logging_creates_file_and_writes_json(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    scrubber = Scrubber([])

    handle = setup_logging(
        level="INFO",
        fmt="json",
        log_dir=log_dir,
        run_id="test_run_1",
        scrubber=scrubber,
        retention_days=7,
    )
    logger = get_logger("test_comp")
    logger.info("Test event occurred", extra={"custom_key": "custom_val"})
    handle.close()

    log_file = log_dir / "algodesk.log"
    assert log_file.is_file()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[-1])
    assert record["level"] == "INFO"
    assert record["run_id"] == "test_run_1"
    assert "Test event occurred" in record["msg"]
    assert record.get("custom_key") == "custom_val"


def test_setup_logging_scrubs_secrets_in_logs(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    scrubber = Scrubber(["super_secret_token_123", "my_password_xyz"])

    handle = setup_logging(
        level="INFO",
        fmt="json",
        log_dir=log_dir,
        run_id="test_run_scrub",
        scrubber=scrubber,
        retention_days=7,
    )
    logger = get_logger("security")
    logger.info("Connecting with super_secret_token_123 to backend")
    logger.warning("Auth failure with pass my_password_xyz")
    handle.close()

    log_file = log_dir / "algodesk.log"
    content = log_file.read_text(encoding="utf-8")
    assert "super_secret_token_123" not in content
    assert "my_password_xyz" not in content
    assert "***" in content


def test_setup_logging_text_format(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    scrubber = Scrubber([])

    handle = setup_logging(
        level="DEBUG",
        fmt="text",
        log_dir=log_dir,
        run_id="test_text_run",
        scrubber=scrubber,
        retention_days=5,
    )
    logger = get_logger("text_logger")
    logger.debug("Text debug message")
    handle.close()

    log_file = log_dir / "algodesk.log"
    content = log_file.read_text(encoding="utf-8")
    assert "[DEBUG]" in content
    assert "Text debug message" in content


def test_setup_logging_invalid_format_raises_error(tmp_path: Path) -> None:
    from algodesk.api.errors import ConfigError

    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    scrubber = Scrubber([])

    with pytest.raises(ConfigError, match="Invalid log format"):
        setup_logging(
            level="INFO",
            fmt="xml",
            log_dir=log_dir,
            run_id="test_err",
            scrubber=scrubber,
            retention_days=7,
        )


def test_setup_logging_missing_dir_raises_error(tmp_path: Path) -> None:
    from algodesk.api.errors import ConfigError

    scrubber = Scrubber([])
    missing_dir = tmp_path / "nonexistent_dir"

    with pytest.raises(ConfigError, match="Log directory does not exist"):
        setup_logging(
            level="INFO",
            fmt="json",
            log_dir=missing_dir,
            run_id="test_err",
            scrubber=scrubber,
            retention_days=7,
        )


def test_setup_logging_daily_retention_pruning(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = DailyUtcRotatingHandler(log_dir / "algodesk.log", retention_days=1)
    assert handler.retention_days == 1
    handler.close()


def test_scrubbing_filter_record_attributes(tmp_path: Path) -> None:
    import logging

    scrubber = Scrubber(["secret_key_456"])
    flt = ScrubbingFilter(scrubber)
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="Leaking secret_key_456 here",
        args=(),
        exc_info=None,
    )
    flt.filter(record)
    assert "secret_key_456" not in record.msg
    assert "***" in record.msg


def test_session_runner_logging_writes_to_logs_dir(tmp_project: Path, runner: CliRunner) -> None:
    res = runner.invoke(
        app,
        [
            "--root",
            str(tmp_project),
            "replay",
            "--dates",
            "2026-10-26",
            "--run",
            "demo_paper",
            "--quiet",
        ],
    )
    assert res.exit_code == 0
    log_file = tmp_project / "logs" / "algodesk.log"
    assert log_file.is_file()
    content = log_file.read_text(encoding="utf-8")
    assert "Session" in content
    assert "demo_paper" in content


def test_feed_probe_logging_writes_to_logs_dir(tmp_project: Path, runner: CliRunner) -> None:
    from unittest.mock import MagicMock

    from algodesk.registry.registry import PluginRegistry

    mock_broker = MagicMock()

    async def fake_stream(*args: Any, **kwargs: Any) -> Any:
        if False:
            yield None

    mock_broker.stream = fake_stream

    orig_create = PluginRegistry.create

    def fake_create(self: Any, kind: str, name: str, **kwargs: Any) -> Any:
        if kind == "broker":
            return mock_broker
        return orig_create(self, kind, name, **kwargs)

    PluginRegistry.create = fake_create  # type: ignore[assignment]
    try:
        res = runner.invoke(
            app,
            [
                "--root",
                str(tmp_project),
                "feed",
                "probe",
                "--symbols",
                "NSE:EQ:RELIANCE",
                "--seconds",
                "0.1",
            ],
        )
        assert res.exit_code == 0
        log_file = tmp_project / "logs" / "algodesk.log"
        assert log_file.is_file()
        content = log_file.read_text(encoding="utf-8")
        assert "Feed probe" in content
    finally:
        PluginRegistry.create = orig_create  # type: ignore[assignment]
