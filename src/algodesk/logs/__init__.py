"""Structured JSON logging with secret scrubbing."""

from algodesk.logs.setup import LoggingHandle, get_logger, setup_logging

__all__ = [
    "LoggingHandle",
    "get_logger",
    "setup_logging",
]
