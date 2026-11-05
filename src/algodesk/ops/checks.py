"""Core platform doctor diagnostic checks (read-only)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from algodesk.api.checks import CheckResult, DoctorCheck, Severity
from algodesk.api.errors import ConfigError
from algodesk.api.kinds import Mode, PluginKind
from algodesk.journal.store import unclosed_runs, verify_journal
from algodesk.params.store import SCHEMA_VERSION
from algodesk.registry.base_checks import registry_doctor_checks

if TYPE_CHECKING:
    from algodesk.ops.runtime import Runtime


def _satisfies_requires_python(ver: tuple[int, int, int], spec: str) -> bool:
    ops: dict[str, Callable[[tuple[int, ...]], bool]] = {
        ">=": lambda t: ver[: len(t)] >= t,
        "<=": lambda t: ver[: len(t)] <= t,
        "<": lambda t: ver[: len(t)] < t,
        ">": lambda t: ver[: len(t)] > t,
        "==": lambda t: ver[: len(t)] == t,
    }
    for clause in (c.strip() for c in spec.split(",") if c.strip()):
        for op, fn in ops.items():
            if clause.startswith(op):
                t = tuple(int(x) for x in clause[len(op) :].rstrip(".*").split("."))
                if not fn(t):
                    return False
                break
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
            return CheckResult(
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
        return CheckResult(
            ok=False,
            message=f"Directories are not writable: {names}",
            fix="Grant write permissions to workspace directories",
            severity=Severity.CRITICAL,
        )

    if missing:
        names = ", ".join(str(p) for p in missing)
        return CheckResult(
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
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='param_history';"
            )
            if cur.fetchone():
                cur = conn.execute(
                    """
                    SELECT h.name, h.scope, h.reason
                    FROM param_history h
                    INNER JOIN (
                        SELECT name, scope, MAX(id) as max_id
                        FROM param_history
                        GROUP BY name, scope
                    ) latest ON h.id = latest.max_id
                    WHERE h.status = 'invalidated';
                    """
                )
                inv_row = cur.fetchone()
                if inv_row:
                    return CheckResult(
                        ok=False,
                        severity=Severity.WARNING,
                        message=f"Parameter override invalidated: {inv_row[0]}@{inv_row[1]}",
                        fix="Review parameter overrides and update configuration",
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


def _find_shipped_core_toml() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "params" / "core.toml"
        if candidate.is_file():
            return candidate
    return None


def _read_toml_param(path: Path, name: str) -> Any:
    if path.is_file():
        try:
            for item in tomllib.loads(path.read_text("utf-8")).get("param", []):
                if item.get("name") == name:
                    return item.get("default")
        except Exception:
            pass
    return None


def _resolve_min_free_disk_gb(runtime: Runtime) -> float:
    if runtime.params is not None:
        try:
            if (val := runtime.params.get("ops.min_free_disk_gb")) is not None:
                return float(val)
        except Exception:
            pass
    core = runtime.root / "config" / "params" / "core.toml"
    p = core if core.is_file() else (_find_shipped_core_toml() or core)
    if (v := _read_toml_param(p, "ops.min_free_disk_gb")) is not None:
        return float(v)
    raise ConfigError(
        what="Missing required parameter 'ops.min_free_disk_gb'",
        where="ops.disk",
        fix="Define ops.min_free_disk_gb in config/params/core.toml",
    )


def _resolve_max_clock_offset_ms(runtime: Runtime) -> int:
    if runtime.params is not None:
        try:
            if (val := runtime.params.get("ops.max_clock_offset_ms")) is not None:
                return int(val)
        except Exception:
            pass
    rt_toml = runtime.root / "config" / "params" / "runtime.toml"
    v = _read_toml_param(rt_toml, "ops.max_clock_offset_ms")
    return int(v) if v is not None else 500


def _check_disk_free(runtime: Runtime) -> CheckResult:
    required_gb = _resolve_min_free_disk_gb(runtime)

    try:
        usage = shutil.disk_usage(runtime.root)
        free_gb = usage.free / (1024**3)
        if free_gb >= required_gb:
            return CheckResult(
                ok=True,
                message=(
                    f"Free disk space {free_gb:.1f} GB meets guidance (>= {required_gb:.1f} GB)"
                ),
            )
        return CheckResult(
            ok=False,
            message=f"Free disk space {free_gb:.1f} GB is below minimum {required_gb:.1f} GB",
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
    C = Severity.CRITICAL
    return [
        DoctorCheck(
            id="python.version", severity=C, run=lambda: _check_python_version(runtime.root)
        ),
        DoctorCheck(id="env.isolated", severity=C, run=lambda: _check_env_isolated(runtime.root)),
        DoctorCheck(id="config.valid", severity=C, run=lambda: _check_config_valid(runtime)),
        DoctorCheck(id="paths.ready", severity=C, run=lambda: _check_paths_ready(runtime)),
        DoctorCheck(id="params.db", severity=C, run=lambda: _check_params_db(runtime)),
    ]


def _check_ticks_projection(runtime: Runtime) -> CheckResult:
    """Doctor check: warn when projected 10 days of ticks exceed registry fraction of free disk."""
    fraction = 0.5
    if runtime.params is not None:
        try:
            val = runtime.params.get("recorder.max_disk_fraction")
            if val is not None:
                fraction = float(val)
        except Exception:
            pass
    if fraction == 0.5:
        data_toml = runtime.root / "config" / "params" / "data.toml"
        v = _read_toml_param(data_toml, "recorder.max_disk_fraction")
        if v is not None:
            fraction = float(v)

    try:
        usage = shutil.disk_usage(runtime.root)
        free_bytes = float(usage.free)
        warn_limit_bytes = free_bytes * fraction

        proj_day_bytes: float | None = None
        results_dir = runtime.paths.results
        if results_dir.is_dir():
            for m_path in sorted(results_dir.glob("*/metrics.json"), reverse=True):
                try:
                    m_data = json.loads(m_path.read_text(encoding="utf-8"))
                    if (
                        "projected_full_day_mb" in m_data
                        and float(m_data["projected_full_day_mb"]) > 0
                    ):
                        proj_day_bytes = float(m_data["projected_full_day_mb"]) * 1024.0 * 1024.0
                        break
                    rec_bytes = float(m_data.get("recorder_bytes", 0))
                    cov_pct = float(m_data.get("session_coverage_pct", 0.0))
                    if rec_bytes > 0 and cov_pct > 0.0:
                        proj_day_bytes = rec_bytes / (cov_pct / 100.0)
                        break
                except Exception:
                    pass

        if proj_day_bytes is None:
            num_inst = 6
            proj_day_bytes = num_inst * 50.0 * 1024.0 * 1024.0

        proj_10d_bytes = proj_day_bytes * 10.0
        proj_10d_gb = proj_10d_bytes / (1024.0**3)
        free_gb = free_bytes / (1024.0**3)

        if proj_10d_bytes > warn_limit_bytes:
            return CheckResult(
                ok=False,
                message=(
                    f"Projected 10-day tick volume ({proj_10d_gb:.2f} GB) exceeds "
                    f"{fraction:.0%} of free disk space ({free_gb:.1f} GB)"
                ),
                fix="Free up disk space or adjust recorder.max_disk_fraction in parameter registry",
            )
        return CheckResult(
            ok=True,
            message=(
                f"Projected 10-day tick volume ({proj_10d_gb:.2f} GB) is within "
                f"{fraction:.0%} of free disk space ({free_gb:.1f} GB)"
            ),
        )
    except Exception as exc:
        return CheckResult(
            ok=False,
            message=f"Failed to check disk ticks projection: {exc}",
            fix="Inspect disk usage on host machine",
        )


def _build_journal_and_disk_checks(runtime: Runtime) -> list[DoctorCheck]:
    W, C = Severity.WARNING, Severity.CRITICAL
    return [
        DoctorCheck(id="disk.free", severity=W, run=lambda: _check_disk_free(runtime)),
        DoctorCheck(
            id="disk.ticks_projection",
            severity=W,
            run=lambda: _check_ticks_projection(runtime),
        ),
        DoctorCheck(
            id="journal.integrity", severity=C, run=lambda: _check_journal_integrity(runtime)
        ),
        DoctorCheck(
            id="journal.closed_cleanly",
            severity=W,
            run=lambda: _check_journal_closed_cleanly(runtime),
        ),
    ]


def _check_data_verify_quick(runtime: Runtime) -> CheckResult:
    data_dir = runtime.paths.data
    manifest_db = data_dir / "state" / "data_manifest.db"
    if not manifest_db.exists() and (data_dir / "manifest.db").exists():
        manifest_db = data_dir / "manifest.db"
    if not manifest_db.exists():
        return CheckResult(ok=True, message="Data manifest absent (will be created on first write)")
    try:
        from algodesk.data.manifest import DataManifest

        m = DataManifest(manifest_db)
        files = m.get_files()
        parts = m.get_parts()
        m.close()
        return CheckResult(
            ok=True,
            message=f"Data store ready ({len(files)} tick parts, {len(parts)} bar parts)",
        )
    except Exception as exc:
        return CheckResult(
            ok=False,
            message=f"Data manifest check failed: {exc}",
            fix="Inspect data_manifest.db or run 'algodesk data verify'",
        )


def _check_data_quarantine(runtime: Runtime) -> CheckResult:
    data_dir = runtime.paths.data
    manifest_db = data_dir / "state" / "data_manifest.db"
    if not manifest_db.exists() and (data_dir / "manifest.db").exists():
        manifest_db = data_dir / "manifest.db"
    q_dir = data_dir / ".quarantine"
    unpurged_count = 0
    if manifest_db.exists():
        try:
            from algodesk.data.manifest import DataManifest

            m = DataManifest(manifest_db)
            q_items = m.get_quarantine(status="quarantined")
            unpurged_count = len(q_items)
            m.close()
        except Exception:
            pass
    elif q_dir.exists():
        unpurged_count = len([d for d in q_dir.iterdir() if d.is_dir()])

    if unpurged_count > 0:
        return CheckResult(
            ok=False,
            message=f"{unpurged_count} quarantined chunk(s) found in data store",
            severity=Severity.WARNING,
            fix="Inspect with 'algodesk data quarantine list' and purge or release",
        )
    return CheckResult(ok=True, message="Quarantine clean (0 items)")


def _check_data_warmup_source(runtime: Runtime) -> CheckResult:
    try:
        brokers = runtime.plugins.list("broker")
    except Exception:
        # pyrefly: ignore[implicit-any-empty-container]
        brokers = []
    src: str | None = None
    if runtime.params is not None:
        try:
            val = runtime.params.get("data.warmup_source")
            if val:
                src = str(val)
        except Exception:
            pass
    if src is None and not brokers:
        return CheckResult(
            ok=True,
            message="No broker plugins configured; warmup source check skipped",
        )
    src_to_check = src or "upstox"
    try:
        p_info = runtime.plugins.info("broker", src_to_check)
        if p_info:
            return CheckResult(
                ok=True,
                message=f"Warm-up data source '{src_to_check}' is valid and registered",
            )
    except Exception:
        pass
    return CheckResult(
        ok=False,
        severity=Severity.CRITICAL,
        message=(
            f"Configured warm-up data source '{src_to_check}' is not registered as a broker plugin"
        ),
        fix=(
            f"Ensure broker plugin '{src_to_check}' exists or configure 'data.warmup_source' "
            "in config/params/data.toml"
        ),
    )


def _check_calendar_coverage_horizon(runtime: Runtime) -> CheckResult:
    from datetime import date

    warn_days = 30
    if runtime.params is not None:
        try:
            val = runtime.params.get("calendar.warn_days")
            if val is not None:
                warn_days = int(val)
        except Exception:
            pass

    today = date.today()
    cal_data_dir = runtime.root / "plugins" / "calendars" / "exchange" / "data"
    if not cal_data_dir.is_dir():
        return CheckResult(ok=True, message="No calendar data directory found")

    expiring: list[str] = []
    for f in sorted(cal_data_dir.glob("*.toml")):
        try:
            with open(f, "rb") as fh:
                data = tomllib.load(fh)
            c_end = data.get("coverage_end")
            exch = data.get("exchange", f.stem)
            if isinstance(c_end, date):
                days_left = (c_end - today).days
                if days_left <= warn_days:
                    expiring.append(f"{exch} ({days_left}d left)")
        except Exception:
            pass

    if expiring:
        exp_list = ", ".join(expiring)
        return CheckResult(
            ok=False,
            severity=Severity.WARNING,
            message=f"Calendar coverage horizon warning: {exp_list} (<= {warn_days} days)",
            fix="Update calendar data TOML with upcoming year holidays",
        )
    return CheckResult(
        ok=True,
        message=f"Calendar coverage valid for all exchanges (> {warn_days} days remaining)",
    )


def _check_market_closure_disagreements(runtime: Runtime) -> CheckResult:
    data_dir = runtime.paths.data
    m_path = data_dir / "state" / "data_manifest.db"
    if not m_path.exists() and (data_dir / "manifest.db").exists():
        m_path = data_dir / "manifest.db"
    if not m_path.exists():
        return CheckResult(ok=True, message="No data manifest found")

    try:
        from algodesk.data.closures import check_market_closure_disagreements
        from algodesk.data.manifest import DataManifest
        from algodesk.ops.context import build_context

        manifest = DataManifest(m_path)
        p_ctx = build_context(Mode.RESEARCH, run_id="doctor_check_closures")
        total = 0
        for exch in ("NSE", "BSE", "MCX"):
            cal_plugin = None
            try:
                cal_plugin = runtime.plugins.create(
                    PluginKind.CALENDAR, "exchange", config={"exchange": exch.lower()}, ctx=p_ctx
                )
            except Exception:
                pass
            from algodesk.api.protocols import Calendar

            cal_typed: Calendar | None = cal_plugin if isinstance(cal_plugin, Calendar) else None
            total += len(check_market_closure_disagreements(manifest, cal_typed, exch))

        if total > 0:
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message=f"{total} unresolved market closure disagreement(s) found",
                fix="Run 'algodesk data closures check' and update calendar data",
            )
    except Exception as exc:
        return CheckResult(
            ok=False,
            severity=Severity.WARNING,
            message=f"Error checking market closure disagreements: {exc}",
            fix="Run 'algodesk data closures check' and inspect manifest or calendar plugins",
        )
    return CheckResult(ok=True, message="No unresolved market closure disagreements")


def _check_legacy_instrument_ids(runtime: Runtime) -> CheckResult:
    try:
        data_dir = runtime.paths.data
        if not data_dir.exists():
            return CheckResult(ok=True, message="No legacy instrument IDs remaining")
        manifest_db = data_dir / "state" / "data_manifest.db"
        if not manifest_db.exists() and (data_dir / "manifest.db").exists():
            manifest_db = data_dir / "manifest.db"
        if not manifest_db.exists():
            return CheckResult(ok=True, message="No legacy instrument IDs remaining")

        from algodesk.data.manifest import DataManifest
        from algodesk.data.migration import build_migration_plan

        manifest = DataManifest(manifest_db)
        plan = build_migration_plan(runtime.paths.data, manifest)
        if plan.total_files > 0:
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message=(
                    f"{plan.total_files} legacy partition file(s) remain across legacy identifiers"
                ),
                fix=(
                    "Run 'algodesk data migrate-ids --apply' then "
                    "'algodesk data migrate-ids --finalize'"
                ),
            )
    except Exception as exc:
        return CheckResult(
            ok=False,
            severity=Severity.WARNING,
            message=f"Error checking legacy instrument IDs: {exc}",
            fix="Inspect manifest and data directories",
        )
    return CheckResult(ok=True, message="No legacy instrument IDs remaining")


def _check_instrument_master_diff(runtime: Runtime) -> CheckResult:
    try:
        data_dir = runtime.paths.data
        if not (data_dir / "instruments").exists():
            return CheckResult(
                ok=True,
                message="Instrument master diff: insufficient history for diff",
            )
        from algodesk.api.alias import resolve_alias
        from algodesk.data.groups import load_groups
        from algodesk.data.instruments_store import InstrumentStore

        store = InstrumentStore(data_dir)
        dates = store.list_dates("upstox")
        if len(dates) < 2:
            return CheckResult(
                ok=True,
                message="Instrument master diff: insufficient history for diff",
            )

        report = store.diff("upstox", dates[-2], dates[-1])
        if not report.has_changes:
            return CheckResult(ok=True, message="Instrument master diff: no changes detected")

        groups_dir = runtime.root / "config" / "groups"
        if not groups_dir.exists():
            return CheckResult(ok=True, message="Instrument master diff: no groups config")
        groups = load_groups(groups_dir)
        all_members: set[str] = set()
        for g_members in groups.values():
            all_members.update(str(resolve_alias(m)) for m in g_members)

        changed_universe = [c for c in report.changed if str(c.id) in all_members]
        removed_universe = [i for i in report.removed if str(i.id) in all_members]

        if changed_universe or removed_universe:
            details = []
            for c in changed_universe:
                f_diffs = [f"{k}: {v[0]}->{v[1]}" for k, v in c.changes.items()]
                details.append(f"{c.id} ({', '.join(f_diffs)})")
            for r in removed_universe:
                details.append(f"{r.id} (REMOVED)")
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message=(
                    "Instrument master diff detected changes to universe members: "
                    f"{'; '.join(details)}"
                ),
                fix="Inspect universe member specifications and parameter constraints",
            )
    except Exception as exc:
        return CheckResult(
            ok=False,
            severity=Severity.WARNING,
            message=f"Error checking instrument master diff: {exc}",
            fix="Inspect data/instruments store",
        )
    return CheckResult(ok=True, message="Instrument master diff: no universe members affected")


def _build_data_doctor_checks(runtime: Runtime) -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id="data.verify_quick",
            severity=Severity.WARNING,
            run=lambda: _check_data_verify_quick(runtime),
        ),
        DoctorCheck(
            id="data.quarantine",
            severity=Severity.WARNING,
            run=lambda: _check_data_quarantine(runtime),
        ),
        DoctorCheck(
            id="data.warmup_source",
            severity=Severity.CRITICAL,
            run=lambda: _check_data_warmup_source(runtime),
        ),
        DoctorCheck(
            id="calendar.coverage_horizon",
            severity=Severity.WARNING,
            run=lambda: _check_calendar_coverage_horizon(runtime),
        ),
        DoctorCheck(
            id="data.closure_disagreements",
            severity=Severity.WARNING,
            run=lambda: _check_market_closure_disagreements(runtime),
        ),
        DoctorCheck(
            id="data.legacy_ids",
            severity=Severity.WARNING,
            run=lambda: _check_legacy_instrument_ids(runtime),
        ),
        DoctorCheck(
            id="instruments.diff_warnings",
            severity=Severity.WARNING,
            run=lambda: _check_instrument_master_diff(runtime),
        ),
    ]


def build_core_checks(runtime: Runtime) -> list[DoctorCheck]:
    """Build all diagnostic doctor checks for the platform runtime (read-only)."""
    checks = _build_env_checks(runtime)
    checks.extend(registry_doctor_checks(runtime.plugins))
    checks.extend(_build_journal_and_disk_checks(runtime))
    checks.extend(_build_data_doctor_checks(runtime))
    return checks


def build_run_checks(runtime: Runtime, run_name: str) -> list[DoctorCheck]:
    """Build diagnostic doctor checks for a specific run configuration."""
    from algodesk.api.kinds import Mode
    from algodesk.config.snapshot import build_effective
    from algodesk.ops.context import build_context

    checks: list[DoctorCheck] = []
    try:
        eff = build_effective(runtime.root, run_name=run_name)
    except Exception as exc:
        err = str(exc)
        checks.append(
            DoctorCheck(
                id=f"run.{run_name}.config",
                severity=Severity.CRITICAL,
                run=lambda: CheckResult(
                    ok=False,
                    message=f"Failed to load run configuration '{run_name}': {err}",
                    fix=f"Verify TOML syntax in config/runs/{run_name}.toml",
                ),
            )
        )
        return checks

    if eff.run is None:
        return checks

    sec_plugin: Any = None

    def _try_plugin(k: str, obj: Any, role: str, c_id: str) -> Any:
        pn = obj.plugin
        cfg = {k: v for k, v in obj.model_dump().items() if k != "plugin"}
        try:
            c = build_context(
                Mode.RESEARCH, run_id=f"doc_{k}_{pn}", params=runtime.params, secrets=sec_plugin
            )
            p = runtime.plugins.create(k, pn, config=cfg, ctx=c)
            if hasattr(p, "doctor_checks"):
                checks.extend(p.doctor_checks())
            return p
        except Exception as exc:
            err = str(exc)

            def _fail() -> CheckResult:
                return CheckResult(
                    ok=False,
                    message=f"Could not initialize {role}: {err}",
                    fix=f"Check {role} configuration",
                )

            checks.append(DoctorCheck(id=c_id, severity=Severity.WARNING, run=_fail))
            return None

    if getattr(eff.run, "secrets", None) is not None:
        sec_plugin = _try_plugin(
            "secrets", eff.run.secrets, "secrets provider", f"run.secrets.{eff.run.secrets.plugin}"
        )
    if getattr(eff.run, "data_source", None) is not None:
        _try_plugin(
            "broker",
            eff.run.data_source,
            "data source plugin",
            f"run.data_source.{eff.run.data_source.plugin}",
        )
    for notif in getattr(eff.run, "notifiers", None) or []:
        _try_plugin(
            "notifier", notif, f"notifier plugin {notif.plugin}", f"run.notifiers.{notif.plugin}"
        )

    # 4. Calendars coverage check (covers today and next 30 days)
    if getattr(eff.run, "calendars", None):

        def _check_calendars() -> CheckResult:
            from datetime import date, timedelta

            today = date.today()
            target_date = today + timedelta(days=30)
            assert eff.run is not None
            for cal in eff.run.calendars:
                c_name = cal.plugin
                ex_name = getattr(cal, "exchange", "nse")
                try:
                    c_ctx = build_context(Mode.RESEARCH, run_id=f"doc_cal_{ex_name}")
                    cal_p = runtime.plugins.create(
                        "calendar", c_name, config={"exchange": ex_name}, ctx=c_ctx
                    )
                    cov_end = getattr(cal_p, "coverage_end", None)
                    if cov_end and cov_end < target_date:
                        return CheckResult(
                            ok=False,
                            severity=Severity.WARNING,
                            message=f"Calendar {ex_name} coverage ends {cov_end} (< 30 days ahead)",
                            fix=f"Update calendar data in plugins/calendars/{c_name}",
                        )
                except Exception as exc:
                    return CheckResult(
                        ok=False,
                        severity=Severity.WARNING,
                        message=f"Could not load calendar for exchange {ex_name}: {exc}",
                        fix="Verify calendar configuration",
                    )
            cal_count = len(eff.run.calendars)
            return CheckResult(
                ok=True,
                message=f"Calendars cover today and next 30 days ({cal_count} exchange(s))",
            )

        checks.append(
            DoctorCheck(
                id="run.calendars",
                severity=Severity.WARNING,
                run=_check_calendars,
            )
        )

    # 5. SNTP clock offset check
    def _check_sntp() -> CheckResult:
        import socket
        import struct
        import time

        server = "time.google.com"
        allowed_offset_ms = _resolve_max_clock_offset_ms(runtime)

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(1.0)
                pkt = bytearray(48)
                pkt[0] = 0x1B
                t1 = time.time()
                s.sendto(pkt, (server, 123))
                data, _ = s.recvfrom(48)
                t4 = time.time()
            sec, frac = struct.unpack("!II", data[40:48])
            t3 = (sec - 2208988800) + (frac / 4294967296.0)
            sec_r, frac_r = struct.unpack("!II", data[32:40])
            t2 = (sec_r - 2208988800) + (frac_r / 4294967296.0)
            offset_ms = abs(((t2 - t1) + (t3 - t4)) / 2.0) * 1000.0
            if offset_ms <= allowed_offset_ms:
                msg = f"Clock offset {offset_ms:.1f} ms meets guidance (<= {allowed_offset_ms} ms)"
                return CheckResult(
                    ok=True,
                    message=msg,
                )
            return CheckResult(
                ok=False,
                severity=Severity.WARNING,
                message=f"Clock offset {offset_ms:.1f} ms exceeds limit {allowed_offset_ms} ms",
                fix="Sync system clock with NTP reference",
            )
        except Exception as exc:
            return CheckResult(
                ok=True,
                message=f"SNTP clock check skipped or offline: {exc}",
            )

    checks.append(
        DoctorCheck(
            id="clock.sntp_offset",
            severity=Severity.WARNING,
            run=_check_sntp,
        )
    )

    # 6. Safety check for paper sessions
    run_mode = getattr(getattr(eff.run, "run", None), "mode", None)
    if run_mode in ("paper", "backtest"):

        def _check_paper_safety() -> CheckResult:
            assert eff.run is not None
            venue = getattr(eff.run, "execution_venue", None)
            v_name = getattr(venue, "plugin", "") if venue else ""
            if v_name != "sim":
                return CheckResult(
                    ok=False,
                    severity=Severity.CRITICAL,
                    message=f"Paper/backtest run cannot use live venue '{v_name}'",
                    fix="Set execution_venue.plugin = 'sim' in run config",
                )
            return CheckResult(
                ok=True,
                message="Paper session safely configured with simulated venue",
            )

        checks.append(
            DoctorCheck(
                id="run.safety.sim_venue",
                severity=Severity.CRITICAL,
                run=_check_paper_safety,
            )
        )

    def _check_plugins_conformance() -> CheckResult:
        from algodesk.registry import get_run_config_plugins

        assert eff.run is not None
        req_plugins = get_run_config_plugins(eff.run)
        missing_or_stale: list[tuple[str, str, str]] = []
        for k, name in req_plugins:
            status = runtime.plugins.conformance_status(k, name)
            if status != "current":
                missing_or_stale.append((str(k.value if hasattr(k, "value") else k), name, status))
        if missing_or_stale:
            first_k, first_n, _ = missing_or_stale[0]
            names_str = ", ".join(f"{k}/{n} ({st})" for k, n, st in missing_or_stale)
            return CheckResult(
                ok=False,
                severity=Severity.CRITICAL,
                message=f"Plugin conformance missing or stale for: {names_str}",
                fix=f"algodesk plugins test {first_k} {first_n}",
            )
        return CheckResult(
            ok=True,
            message=f"All {len(req_plugins)} run plugins have current conformance records",
        )

    checks.append(
        DoctorCheck(
            id="plugins.conformance",
            severity=Severity.CRITICAL,
            run=_check_plugins_conformance,
        )
    )

    return checks
