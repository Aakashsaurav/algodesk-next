"""Doctor diagnostics registry, report model, and formatting."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from algodesk.api.checks import DoctorCheck, Severity


@dataclass(frozen=True)
class DoctorResult:
    """Result of an individual doctor check execution."""

    id: str
    severity: Severity
    ok: bool
    message: str
    fix: str | None
    ms: float


@dataclass(frozen=True)
class DoctorReport:
    """Consolidated report across all executed doctor checks."""

    results: list[DoctorResult]
    passed: int
    warning: int
    critical: int
    exit_code: int


class DoctorRegistry:
    """Registry of diagnostic checks run by algodesk doctor."""

    def __init__(self) -> None:
        self._checks: list[DoctorCheck] = []

    def register(self, check: DoctorCheck) -> None:
        """Register a doctor diagnostic check."""
        self._checks.append(check)

    def register_many(self, checks: Iterable[DoctorCheck]) -> None:
        """Register multiple diagnostic checks."""
        for c in checks:
            self.register(c)

    def run(self, only: str | None = None) -> DoctorReport:
        """Run all registered checks (or filtered by only), never raising."""
        results: list[DoctorResult] = []
        checks_to_run = [c for c in self._checks if only is None or c.id == only]

        for check in checks_to_run:
            t0 = time.perf_counter()
            try:
                res = check.run()
                ms = (time.perf_counter() - t0) * 1000.0
                eff_sev = res.severity if res.severity is not None else check.severity
                results.append(
                    DoctorResult(
                        id=check.id,
                        severity=eff_sev,
                        ok=res.ok,
                        message=res.message,
                        fix=res.fix,
                        ms=ms,
                    )
                )
            except Exception as exc:
                ms = (time.perf_counter() - t0) * 1000.0
                results.append(
                    DoctorResult(
                        id=check.id,
                        severity=Severity.CRITICAL,
                        ok=False,
                        message=f"check crashed: {type(exc).__name__}: {exc}",
                        fix="Inspect check implementation or report a bug",
                        ms=ms,
                    )
                )

        passed = sum(1 for r in results if r.ok)
        warning = sum(1 for r in results if not r.ok and r.severity == Severity.WARNING)
        critical = sum(1 for r in results if not r.ok and r.severity == Severity.CRITICAL)

        exit_code = 2 if critical > 0 else (1 if warning > 0 else 0)

        return DoctorReport(
            results=results,
            passed=passed,
            warning=warning,
            critical=critical,
            exit_code=exit_code,
        )


def format_report(report: DoctorReport, verbose: bool = False) -> str:
    """Format doctor report into human-readable text."""
    header = f"DOCTOR: {report.passed} passed, {report.warning} warning, {report.critical} critical"
    lines = [header]

    if verbose:
        for r in report.results:
            status = "PASS" if r.ok else f"FAIL [{r.severity.value}]"
            lines.append(f"{status} {r.id} ({r.ms:.1f}ms): {r.message}")
            if not r.ok and r.fix:
                lines.append(f"  Fix: {r.fix}")
    else:
        for r in report.results:
            if not r.ok:
                lines.append(f"FAIL [{r.severity.value}] {r.id}: {r.message}")
                if r.fix:
                    lines.append(f"  Fix: {r.fix}")

    return "\n".join(lines)


def report_to_json(report: DoctorReport) -> dict[str, Any]:
    """Convert doctor report into machine-readable JSON dictionary."""
    summary_line = (
        f"DOCTOR: {report.passed} passed, {report.warning} warning, {report.critical} critical"
    )
    return {
        "summary": summary_line,
        "counts": {
            "passed": report.passed,
            "warning": report.warning,
            "critical": report.critical,
        },
        "exit_code": report.exit_code,
        "results": [
            {
                "id": r.id,
                "severity": r.severity.value,
                "ok": r.ok,
                "message": r.message,
                "fix": r.fix,
                "ms": round(r.ms, 2),
            }
            for r in report.results
        ],
    }
