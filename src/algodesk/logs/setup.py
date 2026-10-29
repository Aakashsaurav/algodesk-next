"""Structured logging setup with JSON/text formats, rotation, retention and secret scrubbing.

INV-9: Never leak secrets, tokens, credentials, or sensitive keys into logs.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from algodesk.api.errors import ConfigError
from algodesk.api.scrub import Scrubber

_STANDARD_LOG_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
}


def _iso_ms(created: float) -> str:
    dt = datetime.fromtimestamp(created, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond / 1000):03d}Z"


class ScrubbingFilter(logging.Filter):
    """Filter that masks secrets and credentials in msg, args, extras and exceptions."""

    def __init__(self, scrubber: Scrubber) -> None:
        super().__init__()
        self.scrubber = scrubber

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.scrubber.scrub(record.msg)

        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(self.scrubber.scrub_obj(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = self.scrubber.scrub_obj(record.args)

        for k in list(record.__dict__.keys()):
            if k not in _STANDARD_LOG_ATTRS and not k.startswith("_"):
                val = getattr(record, k)
                setattr(record, k, self.scrubber.scrub_obj(val))

        if record.exc_info:
            raw_tb = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = self.scrubber.scrub(raw_tb)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.scrubber.scrub(record.exc_text)

        return True


class JsonLogFormatter(logging.Formatter):
    """Format log record as JSON line with ts, level, logger, run_id, msg and extras."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": _iso_ms(record.created),
            "level": record.levelname,
            "logger": record.name,
            "run_id": self.run_id,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _STANDARD_LOG_ATTRS and not k.startswith("_"):
                data[k] = v
        if record.exc_text:
            data["exception"] = record.exc_text
        return json.dumps(data, ensure_ascii=False, default=str)


class TextLogFormatter(logging.Formatter):
    """Format log record as human-readable text line."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        ts = _iso_ms(record.created)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STANDARD_LOG_ATTRS and not k.startswith("_")
        }
        extras_str = f" | {extras}" if extras else ""
        prefix = f"{ts} [{record.levelname}] {record.name} (run={self.run_id})"
        msg = f"{prefix}: {record.getMessage()}{extras_str}"
        if record.exc_text:
            msg += f"\n{record.exc_text}"
        return msg


class DailyUtcRotatingHandler(logging.handlers.TimedRotatingFileHandler):
    """Timed rotating file handler at UTC midnight with retention cleanup."""

    def __init__(self, filename: Path | str, retention_days: int) -> None:
        self.retention_days = retention_days
        super().__init__(
            str(filename),
            when="midnight",
            interval=1,
            backupCount=retention_days,
            utc=True,
            encoding="utf-8",
        )

    def doRollover(self) -> None:
        super().doRollover()
        self.prune_retention()

    def prune_retention(self) -> None:
        cutoff = time.time() - (self.retention_days * 86400)
        base = Path(self.baseFilename)
        for p in base.parent.glob(f"{base.name}.*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                pass


class LoggingHandle:
    """Handle for active logging configuration."""

    def __init__(
        self,
        path: Path,
        logger: logging.Logger,
        handlers: list[logging.Handler],
    ) -> None:
        self.path = path
        self.logger = logger
        self.handlers = handlers
        self._closed = False

    def close(self) -> None:
        """Close handlers and remove from logger."""
        if self._closed:
            return
        for h in self.handlers:
            try:
                self.logger.removeHandler(h)
                h.close()
            except Exception:
                pass
        self._closed = True

    def __enter__(self) -> LoggingHandle:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def _parse_log_level(level: str | int) -> int:
    if isinstance(level, str):
        level_name = level.upper()
        if hasattr(logging, level_name):
            val = getattr(logging, level_name)
            if isinstance(val, int):
                return val
        raise ConfigError(
            what=f"Invalid log level '{level}'",
            where="logs.setup",
            fix="Use a valid log level such as DEBUG, INFO, WARNING, ERROR, or CRITICAL",
        )
    if isinstance(level, int):
        return level
    raise ConfigError(
        what=f"Invalid log level type '{type(level).__name__}'",
        where="logs.setup",
        fix="Pass log level as a string (e.g. 'INFO') or integer (e.g. logging.INFO)",
    )


def setup_logging(
    *,
    level: str | int,
    fmt: str,
    log_dir: Path | str,
    run_id: str,
    scrubber: Scrubber,
    retention_days: int,
    console: bool = False,
) -> LoggingHandle:
    """Set up structured logging under the 'algodesk' logger."""
    p_log_dir = Path(log_dir).resolve()
    if not p_log_dir.is_dir():
        raise ConfigError(
            what=f"Log directory does not exist: {p_log_dir}",
            where="logs.setup",
            fix="Create the log directory before calling setup_logging()",
        )

    if fmt not in ("json", "text"):
        raise ConfigError(
            what=f"Invalid log format '{fmt}': expected 'json' or 'text'",
            where="logs.setup",
            fix="Pass fmt='json' or fmt='text'",
        )

    logger = logging.getLogger("algodesk")
    logger.propagate = False
    log_level = _parse_log_level(level)
    logger.setLevel(log_level)

    # Idempotent: close and remove existing handlers
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()

    formatter: logging.Formatter = (
        JsonLogFormatter(run_id) if fmt == "json" else TextLogFormatter(run_id)
    )
    scrub_filter = ScrubbingFilter(scrubber)

    log_file = p_log_dir / "algodesk.log"
    file_handler = DailyUtcRotatingHandler(log_file, retention_days=retention_days)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(scrub_filter)
    logger.addHandler(file_handler)

    handlers: list[logging.Handler] = [file_handler]
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(scrub_filter)
        logger.addHandler(console_handler)
        handlers.append(console_handler)

    return LoggingHandle(path=log_file, logger=logger, handlers=handlers)


def get_logger(component: str) -> logging.Logger:
    """Return a logger under the 'algodesk.' hierarchy."""
    if component.startswith("algodesk."):
        return logging.getLogger(component)
    return logging.getLogger(f"algodesk.{component}")
