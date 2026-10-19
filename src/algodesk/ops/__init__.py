"""Ops module: workspace paths, runtime composition, context, and diagnostics."""

from __future__ import annotations

from algodesk.ops.checks import build_core_checks
from algodesk.ops.context import DenySecrets, NullJournal, build_context
from algodesk.ops.doctor import (
    DoctorRegistry,
    DoctorReport,
    DoctorResult,
    format_report,
    report_to_json,
)
from algodesk.ops.paths import StatePaths, find_project_root
from algodesk.ops.runtime import Runtime, open_runtime

__all__ = [
    "DenySecrets",
    "DoctorRegistry",
    "DoctorReport",
    "DoctorResult",
    "NullJournal",
    "Runtime",
    "StatePaths",
    "build_context",
    "build_core_checks",
    "find_project_root",
    "format_report",
    "open_runtime",
    "report_to_json",
]
