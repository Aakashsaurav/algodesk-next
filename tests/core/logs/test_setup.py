"""Tests for structured logging setup, JSON/text formats, rotation, and scrubbing (INV-9)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from algodesk.api.errors import ConfigError
from algodesk.api.scrub import Scrubber
from algodesk.logs.setup import DailyUtcRotatingHandler, get_logger, setup_logging


def test_setup_logging_json_format_fields(tmp_path: Path) -> None:
    sc = Scrubber()
    h = setup_logging(
        level="INFO",
        fmt="json",
        log_dir=tmp_path,
        run_id="run_101",
        scrubber=sc,
        retention_days=7,
    )
    logger = get_logger("test_comp")
    logger.info("Event occurred", extra={"order_id": "ord_999", "amount": 500})
    h.close()

    log_text = (tmp_path / "algodesk.log").read_text(encoding="utf-8")
    lines = [line for line in log_text.strip().splitlines() if line]
    assert len(lines) == 1
    data = json.loads(lines[0])

    assert "ts" in data
    assert data["level"] == "INFO"
    assert data["logger"] == "algodesk.test_comp"
    assert data["run_id"] == "run_101"
    assert data["msg"] == "Event occurred"
    assert data["order_id"] == "ord_999"
    assert data["amount"] == 500


def test_setup_logging_text_format(tmp_path: Path) -> None:
    sc = Scrubber()
    h = setup_logging(
        level="INFO",
        fmt="text",
        log_dir=tmp_path,
        run_id="run_text_1",
        scrubber=sc,
        retention_days=7,
    )
    logger = get_logger("text_comp")
    logger.info("Text event occurred", extra={"status": "done"})
    h.close()

    log_text = (tmp_path / "algodesk.log").read_text(encoding="utf-8")
    assert "[INFO]" in log_text
    assert "algodesk.text_comp" in log_text
    assert "(run=run_text_1)" in log_text
    assert "Text event occurred" in log_text
    assert "status': 'done'" in log_text


def test_secret_never_in_output_msg_args_extras_and_exceptions(tmp_path: Path) -> None:
    secret = "s3cr3t-api-key-999"
    sc = Scrubber()
    sc.register(secret)

    h = setup_logging(
        level="DEBUG",
        fmt="json",
        log_dir=tmp_path,
        run_id="run_sec",
        scrubber=sc,
        retention_days=7,
    )
    logger = get_logger("secure_comp")

    # In msg
    logger.info(f"Connecting with {secret}")

    # In args
    logger.info("Token value is %s", secret)

    # In extra
    logger.warning("Warning triggered", extra={"auth_token": secret})

    # In exception
    try:
        raise RuntimeError(f"Database refused key {secret}")
    except RuntimeError:
        logger.error("Operation failed", exc_info=True)

    h.close()

    log_content = (tmp_path / "algodesk.log").read_text(encoding="utf-8")
    assert secret not in log_content
    assert "***" in log_content


def test_idempotent_setup(tmp_path: Path) -> None:
    sc = Scrubber()
    setup_logging(
        level="INFO",
        fmt="json",
        log_dir=tmp_path,
        run_id="r1",
        scrubber=sc,
        retention_days=7,
    )
    logger = logging.getLogger("algodesk")
    assert len(logger.handlers) == 1

    # Call setup again with new run_id
    h2 = setup_logging(
        level="INFO",
        fmt="json",
        log_dir=tmp_path,
        run_id="r2",
        scrubber=sc,
        retention_days=7,
    )
    assert len(logger.handlers) == 1

    h2.close()
    assert len(logger.handlers) == 0


def test_rotation_via_forced_rollover_and_retention_pruning(tmp_path: Path) -> None:
    sc = Scrubber()
    retention_days = 5
    h = setup_logging(
        level="INFO",
        fmt="json",
        log_dir=tmp_path,
        run_id="r_rot",
        scrubber=sc,
        retention_days=retention_days,
    )
    logger = get_logger("rot_comp")
    logger.info("Before rollover")

    # Create old log file older than retention (e.g. 10 days old)
    old_file = tmp_path / "algodesk.log.2026-01-01"
    old_file.write_text("old logs\n", encoding="utf-8")
    past_time = time.time() - (10 * 86400)
    os.utime(old_file, (past_time, past_time))

    # Create recent file (e.g. 2 days old)
    recent_file = tmp_path / "algodesk.log.2026-10-16"
    recent_file.write_text("recent logs\n", encoding="utf-8")
    recent_time = time.time() - (2 * 86400)
    os.utime(recent_file, (recent_time, recent_time))

    handler = [hnd for hnd in h.handlers if isinstance(hnd, DailyUtcRotatingHandler)][0]
    handler.doRollover()

    # The old file should be pruned, recent should remain
    assert not old_file.exists()
    assert recent_file.exists()

    h.close()


def test_level_filtering(tmp_path: Path) -> None:
    sc = Scrubber()
    h = setup_logging(
        level="WARNING",
        fmt="json",
        log_dir=tmp_path,
        run_id="r_filt",
        scrubber=sc,
        retention_days=7,
    )
    logger = get_logger("filt")
    logger.debug("Debug msg")
    logger.info("Info msg")
    logger.warning("Warning msg")
    logger.error("Error msg")
    h.close()

    lines = [
        json.loads(line)
        for line in (tmp_path / "algodesk.log").read_text(encoding="utf-8").strip().splitlines()
        if line
    ]
    levels = [entry["level"] for entry in lines]
    assert levels == ["WARNING", "ERROR"]


def test_missing_log_dir_raises_config_error(tmp_path: Path) -> None:
    missing_dir = tmp_path / "nonexistent_logs_folder"
    sc = Scrubber()
    with pytest.raises(ConfigError) as exc_info:
        setup_logging(
            level="INFO",
            fmt="json",
            log_dir=missing_dir,
            run_id="r_err",
            scrubber=sc,
            retention_days=7,
        )
    assert "Log directory does not exist" in exc_info.value.what
    assert exc_info.value.fix != ""


def test_invalid_fmt_raises_config_error(tmp_path: Path) -> None:
    sc = Scrubber()
    with pytest.raises(ConfigError) as exc_info:
        setup_logging(
            level="INFO",
            fmt="yaml",
            log_dir=tmp_path,
            run_id="r_err",
            scrubber=sc,
            retention_days=7,
        )
    assert "Invalid log format" in exc_info.value.what


def test_console_logging(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sc = Scrubber()
    h = setup_logging(
        level="INFO",
        fmt="json",
        log_dir=tmp_path,
        run_id="r_console",
        scrubber=sc,
        retention_days=7,
        console=True,
    )
    logger = get_logger("console_comp")
    logger.info("Console output test")
    h.close()

    captured = capsys.readouterr()
    assert "Console output test" in captured.out
    assert "algodesk.console_comp" in captured.out


def test_get_logger_prefix() -> None:
    l1 = get_logger("my_component")
    assert l1.name == "algodesk.my_component"

    l2 = get_logger("algodesk.already_prefixed")
    assert l2.name == "algodesk.already_prefixed"


def test_logging_handle_context_manager(tmp_path: Path) -> None:
    sc = Scrubber()
    with setup_logging(
        level="INFO",
        fmt="json",
        log_dir=tmp_path,
        run_id="r_ctx",
        scrubber=sc,
        retention_days=7,
    ) as h:
        assert h.path.exists()
        assert len(h.logger.handlers) == 1
    assert len(h.logger.handlers) == 0


def test_json_formatter_special_chars_in_msg(tmp_path: Path) -> None:
    sc = Scrubber()
    h = setup_logging(
        level="INFO", fmt="json", log_dir=tmp_path, run_id="r_spec", scrubber=sc, retention_days=7
    )
    logger = get_logger("special")
    logger.info('Message with "quotes", \n newline, and \t tabs')
    h.close()

    lines = (tmp_path / "algodesk.log").read_text(encoding="utf-8").strip().splitlines()
    data = json.loads(lines[0])
    assert 'Message with "quotes"' in data["msg"]


def test_json_formatter_numeric_and_bool_extras(tmp_path: Path) -> None:
    sc = Scrubber()
    h = setup_logging(
        level="INFO", fmt="json", log_dir=tmp_path, run_id="r_num", scrubber=sc, retention_days=7
    )
    logger = get_logger("num_comp")
    logger.info("Event", extra={"flag": True, "count": 42, "ratio": 3.14})
    h.close()

    lines = (tmp_path / "algodesk.log").read_text(encoding="utf-8").strip().splitlines()
    data = json.loads(lines[0])
    assert data["flag"] is True
    assert data["count"] == 42
    assert data["ratio"] == 3.14


def test_scrubber_filter_scrubs_dict_args(tmp_path: Path) -> None:
    sc = Scrubber()
    sc.register("secret_dict_arg_val")
    h = setup_logging(
        level="INFO",
        fmt="json",
        log_dir=tmp_path,
        run_id="r_dict_args",
        scrubber=sc,
        retention_days=7,
    )
    logger = get_logger("dict_arg_comp")
    logger.info("Hello %(user)s", {"user": "secret_dict_arg_val"})
    h.close()

    log_content = (tmp_path / "algodesk.log").read_text(encoding="utf-8")
    assert "secret_dict_arg_val" not in log_content
    assert "***" in log_content


def test_scrubber_filter_scrubs_nested_extra_objects(tmp_path: Path) -> None:
    sc = Scrubber()
    sc.register("secret_nested_extra")
    h = setup_logging(
        level="INFO", fmt="json", log_dir=tmp_path, run_id="r_nest", scrubber=sc, retention_days=7
    )
    logger = get_logger("nest_comp")
    logger.info("Action", extra={"meta": {"items": ["safe", "secret_nested_extra"]}})
    h.close()

    log_content = (tmp_path / "algodesk.log").read_text(encoding="utf-8")
    assert "secret_nested_extra" not in log_content
    assert "***" in log_content


def test_scrubber_filter_scrubs_multiline_traceback(tmp_path: Path) -> None:
    sc = Scrubber()
    sc.register("secret_traceback_token")
    h = setup_logging(
        level="INFO", fmt="json", log_dir=tmp_path, run_id="r_tb", scrubber=sc, retention_days=7
    )
    logger = get_logger("tb_comp")
    try:
        raise ValueError("Error with secret_traceback_token in frame")
    except ValueError:
        logger.error("Exception caught", exc_info=True)
    h.close()

    log_content = (tmp_path / "algodesk.log").read_text(encoding="utf-8")
    assert "secret_traceback_token" not in log_content
    assert "***" in log_content


def test_text_formatter_with_and_without_extras(tmp_path: Path) -> None:
    sc = Scrubber()
    h = setup_logging(
        level="INFO",
        fmt="text",
        log_dir=tmp_path,
        run_id="r_txt_both",
        scrubber=sc,
        retention_days=7,
    )
    logger = get_logger("txt_both")
    logger.info("Message without extra")
    logger.info("Message with extra", extra={"tag": "v1"})
    h.close()

    lines = (tmp_path / "algodesk.log").read_text(encoding="utf-8").strip().splitlines()
    assert "Message without extra" in lines[0]
    assert " | {'tag': 'v1'}" in lines[1]


def test_text_formatter_with_exception(tmp_path: Path) -> None:
    sc = Scrubber()
    h = setup_logging(
        level="INFO",
        fmt="text",
        log_dir=tmp_path,
        run_id="r_txt_exc",
        scrubber=sc,
        retention_days=7,
    )
    logger = get_logger("txt_exc")
    try:
        raise RuntimeError("Text format error occurred")
    except RuntimeError:
        logger.error("Failure", exc_info=True)
    h.close()

    log_content = (tmp_path / "algodesk.log").read_text(encoding="utf-8")
    assert "RuntimeError: Text format error occurred" in log_content


def test_setup_logging_integer_log_level(tmp_path: Path) -> None:
    sc = Scrubber()
    h = setup_logging(
        level=logging.ERROR,
        fmt="json",
        log_dir=tmp_path,
        run_id="r_int_lvl",
        scrubber=sc,
        retention_days=7,
    )
    logger = get_logger("int_lvl")
    logger.warning("Should not be logged")
    logger.error("Should be logged")
    h.close()

    lines = (tmp_path / "algodesk.log").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert "Should be logged" in lines[0]


def test_setup_logging_close_idempotent(tmp_path: Path) -> None:
    sc = Scrubber()
    h = setup_logging(
        level="INFO", fmt="json", log_dir=tmp_path, run_id="r_idem", scrubber=sc, retention_days=7
    )
    h.close()
    h.close()
    assert h._closed is True
