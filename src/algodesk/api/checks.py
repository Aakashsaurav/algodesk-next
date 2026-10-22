"""Doctor check types and severity levels for AlgoDesk plugins and core."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    """Severity level of a doctor check failure."""

    CRITICAL = "critical"
    WARNING = "warning"


@dataclass(frozen=True)
class CheckResult:
    """Result of running a doctor check."""

    ok: bool
    message: str
    fix: str | None = None
    severity: Severity | None = None


@dataclass(frozen=True)
class DoctorCheck:
    """A diagnostic check executed by ``algodesk doctor``."""

    id: str
    severity: Severity
    run: Callable[[], CheckResult]
