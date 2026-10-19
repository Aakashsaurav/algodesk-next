"""Core platform doctor diagnostic checks (read-only)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from algodesk.api.checks import CheckResult, DoctorCheck, Severity
from algodesk.journal.store import unclosed_runs, verify_journal
from algodesk.params.store import SCHEMA_VERSION
from algodesk.registry.base_checks import registry_doctor_checks

if TYPE_CHECKING:
    from algodesk.ops.runtime import Runtime


@dataclass(frozen=True)
class CheckResultWithSeverity(CheckResult):
    """CheckResult with custom severity overriding the DoctorCheck default."""

    severity: Severity | None = None


def _satisfies_requires_python(ver: tuple[int, int, int], spec: str) -> bool:
    clauses = [c.strip() for c in spec.split(",") if c.strip()]
    for clause in clauses:
        if clause.startswith(">="):
            target = tuple(int(x) for x in clause[2:].split("."))
            if ver[: len(target)] < target:
                return False
        elif clause.startswith("<="):
            target = tuple(int(x) for x in clause[2:].split("."))
            if ver[: len(target)] > target:
                return False
        elif clause.startswith("<"):
            target = tuple(int(x) for x in clause[1:].split("."))
            if ver[: len(target)] >= target:
                return False
        elif clause.startswith(">"):
            target = tuple(int(x) for x in clause[1:].split("."))
            if ver[: len(target)] <= target:
                return False
        elif clause.startswith("=="):
            raw = clause[2:].rstrip(".*")
            target = tuple(int(x) for x in raw.split("."))
            if ver[: len(target)] != target:
                return False
    return True


def _check_python_version(root: Path) -> CheckResult:
    pyproject = root / "pyproject.toml"
    req_spec = ">=3.13,<3.14"
    if pyproject.is_file():
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            req_spec = str(data.get("project", {}).get("requires-python", req_spec))
        except Exception:
            pass

    ver = sys.version_info
    cur = f"{ver.major}.{ver.minor}.{ver.micro}"
    if _satisfies_requires_python((ver.major, ver.minor, ver.micro), req_spec):
        return CheckResult(ok=True, message=f"Python {cur} satisfies requires-python ({req_spec})")
    return CheckResult(
        ok=False,
        message=f"Python {cur} does not satisfy requires-python ({req_spec})",
        fix=f"Use Python matching '{req_spec}'",
    )


def _check_env_isolated(root: Path) -> CheckResult:
    root_resolved = root.resolve()
    interp_real = Path(os.path.realpath(sys.executable))
    expected_py = root_resolved / ".local" / "python"
    if not (interp_real.is_relative_to(expected_py) or interp_real == expected_py):
        return CheckResult(
            ok=False,
            message=f"Interpreter realpath '{interp_real}' is not under '{expected_py}'",
            fix="Run 'source scripts/env.sh' to use isolated Python",
        )

    prefix_real = Path(os.path.realpath(sys.prefix))
    expected_venv = root_resolved / ".venv"
    if not (prefix_real.is_relative_to(expected_venv) or prefix_real == expected_venv):
        return CheckResult(
            ok=False,
            message=f"Virtual environment prefix '{prefix_real}' is not under '{expected_venv}'",
            fix="Run 'source scripts/env.sh' to activate project virtualenv",
        )

    if os.environ.get("PYTHONNOUSERSITE") != "1":
        return CheckResult(
            ok=False,
            message="PYTHONNOUSERSITE is not set to 1",
            fix="Run 'source scripts/env.sh' to disable user site packages",
        )

    uv_paths = {
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_BIN_DIR",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "UV_PROJECT_ENVIRONMENT",
    }
    for var in uv_paths:
        val = os.environ.get(var)
        if val:
            p = Path(val).resolve()
            if not (p.is_relative_to(root_resolved) or p == root_resolved):
                return CheckResult(
                    ok=False,
                    message=f"{var} points outside project: '{val}'",
                    fix="Run 'source scripts/env.sh' to isolate UV environment",
                )

    return CheckResult(ok=True, message="Environment is fully isolated inside workspace root")


def _check_config_valid(runtime: Runtime) -> CheckResult:
    if runtime.effective is None or runtime.effective.platform is None:
        return CheckResult(
            ok=False,
            message="Platform configuration is not loaded",
            fix="Check config/platform.toml syntax and validity",
        )
    return CheckResult(
        ok=True,
        message=f"Configuration valid (hash {runtime.effective.hash[:16]})",
    )


def _check_paths_ready(runtime: Runtime) -> CheckResult:
    root_resolved = runtime.root.resolve()
    dirs = [
        runtime.paths.data,
        runtime.paths.logs,
        runtime.paths.results,
        runtime.paths.local,
    ]
    missing: list[Path] = []
    unwritable: list[Path] = []

    for d in dirs:
        d_res = d.resolve()
        if not (d_res.is_relative_to(root_resolved) or d_res == root_resolved):
            return CheckResultWithSeverity(
                ok=False,
                message=f"Path '{d}' escapes project root '{root_resolved}'",
                fix="Configure paths to reside inside project workspace",
                severity=Severity.CRITICAL,
            )
        if not d.exists():
            missing.append(d)
        elif not os.access(d, os.W_OK | os.X_OK):
            unwritable.append(d)

    if unwritable:
        names = ", ".join(str(p) for p in unwritable)
        return CheckResultWithSeverity(
            ok=False,
            message=f"Directories are not writable: {names}",
            fix="Grant write permissions to workspace directories",
            severity=Severity.CRITICAL,
        )

    if missing:
        names = ", ".join(str(p) for p in missing)
        return CheckResultWithSeverity(
            ok=False,
            message=f"Directories not created yet: {names}",
            fix="Run runtime with ensure=True or create directories",
            severity=Severity.WARNING,
        )

    return CheckResult(ok=True, message="All runtime paths exist inside project and are writable")


def _check_params_db(runtime: Runtime) -> CheckResult:
    db_path = runtime.state.params_db
    if not db_path.exists():
        return CheckResult(ok=True, message="params.db absent (will be initialized on first write)")

    try:
        uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            cur = conn.execute("PRAGMA quick_check;")
            row = cur.fetchone()
            if not row or row[0] != "ok":
                return CheckResult(
                    ok=False,
                    message="params.db failed quick integrity check",
                    fix="Restore params.db from backup or re-initialize",
                )
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version';"
            )
            if cur.fetchone():
                cur = conn.execute("SELECT MAX(version) FROM schema_version;")
                v_row = cur.fetchone()
                ver = v_row[0] if v_row and v_row[0] is not None else 0
                if ver > SCHEMA_VERSION:
                    return CheckResult(
                        ok=False,
                        message=f"params.db schema version {ver} is newer than supported",
                        fix="Upgrade algodesk",
                    )
        finally:
            conn.close()
        return CheckResult(ok=True, message="params.db opened cleanly with current schema")
    except Exception as exc:
        return CheckResult(
            ok=False,
            message=f"Failed to open params.db: {exc}",
            fix="Restore params.db from backup",
        )


def _check_disk_free(runtime: Runtime) -> CheckResult:
    min_gb = 5.0
    if runtime.params is not None:
        try:
            min_gb = float(runtime.params.get("ops.min_free_disk_gb"))
        except Exception:
            min_gb = 5.0

    try:
        usage = shutil.disk_usage(runtime.root)
        free_gb = usage.free / (1024**3)
        if free_gb >= min_gb:
            return CheckResult(
                ok=True,
                message=f"Free disk space {free_gb:.1f} GB meets guidance (>= {min_gb:.1f} GB)",
            )
        return CheckResult(
            ok=False,
            message=f"Free disk space {free_gb:.1f} GB is below minimum {min_gb:.1f} GB",
            fix="Free up disk space on the volume hosting the workspace",
        )
    except Exception as exc:
        return CheckResult(
            ok=False,
            message=f"Failed to check disk space: {exc}",
            fix="Inspect disk usage on host machine",
        )


def _check_journal_integrity(runtime: Runtime) -> CheckResult:
    j_path = runtime.state.journal_db
    if not j_path.exists():
        return CheckResult(
            ok=True,
            message="journal.db absent (will be created on first write)",
        )

    res = verify_journal(j_path)
    if res.ok:
        return CheckResult(
            ok=True,
            message=f"Journal integrity verified: {res.checked} events",
        )
    return CheckResult(
        ok=False,
        message=f"Journal integrity check failed: {res.reason}",
        fix="Inspect corrupted journal or restore from backup",
    )


def _check_journal_closed_cleanly(runtime: Runtime) -> CheckResult:
    j_path = runtime.state.journal_db
    if not j_path.exists():
        return CheckResult(ok=True, message="journal.db absent")

    unclosed = unclosed_runs(j_path)
    if not unclosed:
        return CheckResult(ok=True, message="All journal runs closed cleanly")

    run_ids = [str(r.get("run_id", r)) for r in unclosed]
    return CheckResult(
        ok=False,
        message=f"{len(unclosed)} unclosed run(s) found in journal: {', '.join(run_ids)}",
        fix="Investigate aborted runs or mark them closed",
    )


def _build_env_checks(runtime: Runtime) -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id="python.version",
            severity=Severity.CRITICAL,
            run=lambda: _check_python_version(runtime.root),
        ),
        DoctorCheck(
            id="env.isolated",
            severity=Severity.CRITICAL,
            run=lambda: _check_env_isolated(runtime.root),
        ),
        DoctorCheck(
            id="config.valid",
            severity=Severity.CRITICAL,
            run=lambda: _check_config_valid(runtime),
        ),
        DoctorCheck(
            id="paths.ready",
            severity=Severity.CRITICAL,
            run=lambda: _check_paths_ready(runtime),
        ),
        DoctorCheck(
            id="params.db",
            severity=Severity.CRITICAL,
            run=lambda: _check_params_db(runtime),
        ),
    ]


def _build_journal_and_disk_checks(runtime: Runtime) -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id="disk.free",
            severity=Severity.WARNING,
            run=lambda: _check_disk_free(runtime),
        ),
        DoctorCheck(
            id="journal.integrity",
            severity=Severity.CRITICAL,
            run=lambda: _check_journal_integrity(runtime),
        ),
        DoctorCheck(
            id="journal.closed_cleanly",
            severity=Severity.WARNING,
            run=lambda: _check_journal_closed_cleanly(runtime),
        ),
    ]


def build_core_checks(runtime: Runtime) -> list[DoctorCheck]:
    """Build all diagnostic doctor checks for the platform runtime (read-only)."""
    checks = _build_env_checks(runtime)
    checks.extend(registry_doctor_checks(runtime.plugins))
    checks.extend(_build_journal_and_disk_checks(runtime))
    return checks
