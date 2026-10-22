"""Codebase size, modules, and dependency complexity budget tool (CB-1..CB-9)."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from algodesk.api.checks import CheckResult, DoctorCheck, Severity
from algodesk.api.errors import ConfigError
from algodesk.api.services import ParamReader

_DEP_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_\-\.]+")


@dataclass(frozen=True)
class PackageBudget:
    """Size and module metrics for a package or plugin."""

    name: str
    kind: str
    lines: int
    modules: int
    status: str


@dataclass(frozen=True)
class BudgetSummary:
    """Summary of overall codebase metrics against guidance and limits."""

    core_lines: int
    core_lines_guidance: int
    core_modules: int
    core_modules_guidance: int
    core_deps: int
    max_core_deps: int
    total_deps: int
    max_total_deps: int
    status: str


@dataclass(frozen=True)
class BudgetReport:
    """Complete budget evaluation report."""

    summary: BudgetSummary
    packages: list[PackageBudget]
    plugins: list[PackageBudget]
    core_dep_names: list[str]
    plugin_dep_names: list[str]
    total_dep_names: list[str]
    exit_code: int


def count_file_lines(path: Path) -> int:
    """Count non-blank, non-comment lines in a source file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip() and not line.strip().startswith("#"))
    except OSError:
        return 0


def _parse_canonical_dep_name(dep_spec: str) -> str:
    m = _DEP_NAME_REGEX.match(dep_spec.strip())
    return m.group(0).lower().replace("_", "-") if m else dep_spec.strip().lower()


def _find_shipped_core_toml() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "params" / "core.toml"
        if candidate.is_file():
            return candidate
    return None


def load_budget_params(
    root: Path, params: ParamReader | Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Load budget guidance and limit parameters from registry or core.toml."""
    res: dict[str, Any] = {}
    keys = [
        "budget.core_lines_guidance",
        "budget.core_modules_guidance",
        "budget.plugin_lines_guidance",
        "budget.warn_overshoot_pct",
        "budget.max_runtime_dependencies",
        "budget.max_core_dependencies",
    ]
    if params is not None:
        for k in keys:
            try:
                if hasattr(params, "get"):
                    v = params.get(k)
                    if v is not None:
                        res[k] = v
                elif isinstance(params, Mapping) and k in params:
                    res[k] = params[k]
            except Exception:
                pass
    core_toml = root / "config" / "params" / "core.toml"
    if core_toml.is_file():
        try:
            data = tomllib.loads(core_toml.read_text(encoding="utf-8"))
            for p in data.get("param", []):
                name = p.get("name")
                if name in keys and name not in res:
                    res[name] = p.get("default")
        except Exception:
            pass

    if any(k not in res for k in keys):
        shipped = _find_shipped_core_toml()
        if shipped and shipped != core_toml:
            try:
                data = tomllib.loads(shipped.read_text(encoding="utf-8"))
                for p in data.get("param", []):
                    name = p.get("name")
                    if name in keys and name not in res:
                        res[name] = p.get("default")
            except Exception:
                pass

    for k in keys:
        if k not in res:
            raise ConfigError(
                what=f"Missing required parameter '{k}'",
                where="budget",
                fix="Define it in config/params/core.toml or register in ParamRegistry",
            )
    return res


def evaluate_metric_status(val: int | float, guidance: int | float, overshoot_pct: float) -> str:
    """Return metric status: ok, advisory, or review."""
    if val <= guidance:
        return "ok"
    if val <= guidance * (1.0 + overshoot_pct / 100.0):
        return "advisory"
    return "review"


def _scan_single_py_file(path: Path) -> tuple[int, int]:
    cnt = count_file_lines(path)
    if path.name == "__init__.py" and cnt == 0:
        return 0, 0
    return cnt, 1


def scan_core_packages(src_dir: Path, overshoot_pct: float) -> tuple[list[PackageBudget], int, int]:
    """Scan all packages under src/algodesk, excluding devtools."""
    packages: list[PackageBudget] = []
    total_lines = 0
    total_mods = 0
    if not src_dir.is_dir():
        return packages, 0, 0

    root_lines, root_mods = 0, 0
    for f in sorted(src_dir.glob("*.py")):
        cnt, mod = _scan_single_py_file(f)
        root_lines += cnt
        root_mods += mod

    if root_mods > 0:
        packages.append(
            PackageBudget(
                name="(root)", kind="core", lines=root_lines, modules=root_mods, status="ok"
            )
        )
        total_lines += root_lines
        total_mods += root_mods

    for d in sorted(src_dir.iterdir()):
        if not d.is_dir() or d.name == "devtools" or d.name == "__pycache__":
            continue
        pkg_lines, pkg_mods = 0, 0
        for py in sorted(d.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            cnt, mod = _scan_single_py_file(py)
            pkg_lines += cnt
            pkg_mods += mod

        total_lines += pkg_lines
        total_mods += pkg_mods
        packages.append(
            PackageBudget(
                name=d.name,
                kind="core",
                lines=pkg_lines,
                modules=pkg_mods,
                status="ok",
            )
        )
    return packages, total_lines, total_mods


def scan_plugins(
    plugins_dir: Path, plugin_guidance: int, overshoot_pct: float
) -> tuple[list[PackageBudget], list[str]]:
    """Scan all plugins under plugins/ and gather required packages."""
    plugin_budgets: list[PackageBudget] = []
    required_packages: list[str] = []
    if not plugins_dir.is_dir():
        return plugin_budgets, required_packages

    for manifest_path in sorted(plugins_dir.rglob("plugin.toml")):
        p_dir = manifest_path.parent
        rel_name = p_dir.relative_to(plugins_dir).as_posix()
        p_lines, p_mods = 0, 0
        for py in sorted(p_dir.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            cnt, mod = _scan_single_py_file(py)
            p_lines += cnt
            p_mods += mod

        try:
            m_data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
            pkgs = m_data.get("requires", {}).get("packages", [])
            for pkg in pkgs:
                if isinstance(pkg, str) and pkg.strip():
                    required_packages.append(_parse_canonical_dep_name(pkg))
        except Exception:
            pass

        st = evaluate_metric_status(p_lines, plugin_guidance, overshoot_pct)
        plugin_budgets.append(
            PackageBudget(
                name=rel_name,
                kind="plugin",
                lines=p_lines,
                modules=p_mods,
                status=st,
            )
        )
    return plugin_budgets, sorted(set(required_packages))


def extract_pyproject_dependencies(pyproject_path: Path) -> list[str]:
    """Extract runtime dependencies declared in pyproject.toml."""
    if not pyproject_path.is_file():
        return []
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        deps = data.get("project", {}).get("dependencies", [])
        return sorted({_parse_canonical_dep_name(d) for d in deps if isinstance(d, str)})
    except Exception:
        return []


def run_budget(root: Path, params: ParamReader | Mapping[str, Any] | None = None) -> BudgetReport:
    """Calculate and evaluate codebase budget metrics against guidance and limits."""
    bp = load_budget_params(root, params)
    core_lines_guidance = int(bp["budget.core_lines_guidance"])
    core_mods_guidance = int(bp["budget.core_modules_guidance"])
    plugin_guidance = int(bp["budget.plugin_lines_guidance"])
    overshoot_pct = float(bp["budget.warn_overshoot_pct"])
    max_core_deps = int(bp["budget.max_core_dependencies"])
    max_runtime_deps = int(bp["budget.max_runtime_dependencies"])

    src_dir = root / "src" / "algodesk"
    packages, total_lines, total_mods = scan_core_packages(src_dir, overshoot_pct)

    plugins_dir = root / "plugins"
    plugins, plugin_deps = scan_plugins(plugins_dir, plugin_guidance, overshoot_pct)

    core_deps = extract_pyproject_dependencies(root / "pyproject.toml")
    total_deps = sorted(set(core_deps) | set(plugin_deps))

    lines_status = evaluate_metric_status(total_lines, core_lines_guidance, overshoot_pct)
    mods_status = evaluate_metric_status(total_mods, core_mods_guidance, overshoot_pct)
    dep_fail = len(core_deps) > max_core_deps or len(total_deps) > max_runtime_deps

    has_advisory = (
        lines_status != "ok" or mods_status != "ok" or any(p.status != "ok" for p in plugins)
    )

    if dep_fail:
        overall_status = "FAIL"
        exit_code = 1
    elif has_advisory:
        overall_status = "ADVISORY"
        exit_code = 0
    else:
        overall_status = "OK"
        exit_code = 0

    summary = BudgetSummary(
        core_lines=total_lines,
        core_lines_guidance=core_lines_guidance,
        core_modules=total_mods,
        core_modules_guidance=core_mods_guidance,
        core_deps=len(core_deps),
        max_core_deps=max_core_deps,
        total_deps=len(total_deps),
        max_total_deps=max_runtime_deps,
        status=overall_status,
    )

    return BudgetReport(
        summary=summary,
        packages=packages,
        plugins=plugins,
        core_dep_names=core_deps,
        plugin_dep_names=plugin_deps,
        total_dep_names=total_deps,
        exit_code=exit_code,
    )


def format_budget_text(report: BudgetReport) -> str:
    """Format budget report as required text table."""
    s = report.summary
    first_line = (
        f"BUDGET: core {s.core_lines} lines (guidance {s.core_lines_guidance}) | "
        f"{s.core_modules} modules (guidance {s.core_modules_guidance}) | "
        f"deps {s.core_deps}/{s.max_core_deps} core, "
        f"{s.total_deps}/{s.max_total_deps} total | {s.status}"
    )

    lines = [first_line, ""]
    hdr = f"{'Package / Plugin':<30} {'Type':<8} {'Lines':>8} {'Modules':>8} {'Status':<10}"
    lines.append(hdr)
    lines.append("-" * len(hdr))

    for p in report.packages:
        lines.append(f"{p.name:<30} {p.kind:<8} {p.lines:>8} {p.modules:>8} {p.status:<10}")

    for pl in report.plugins:
        lines.append(f"{pl.name:<30} {pl.kind:<8} {pl.lines:>8} {pl.modules:>8} {pl.status:<10}")

    lines.append("-" * len(hdr))
    lines.append(
        f"{'Total core':<30} {'core':<8} {s.core_lines:>8} {s.core_modules:>8} {s.status:<10}"
    )
    return "\n".join(lines)


def format_budget_json(report: BudgetReport) -> str:
    """Format budget report as JSON."""
    data = {
        "summary": asdict(report.summary),
        "packages": [asdict(p) for p in report.packages],
        "plugins": [asdict(pl) for pl in report.plugins],
        "dependencies": {
            "core": report.core_dep_names,
            "plugins": report.plugin_dep_names,
            "total": report.total_dep_names,
        },
    }
    return json.dumps(data, indent=2)


def make_budget_checks(runtime: Any) -> list[DoctorCheck]:
    """Create doctor diagnostic checks for dependencies (CRITICAL) and size (WARNING)."""
    root = Path(runtime.root)
    params = getattr(runtime, "params", None)

    def check_deps() -> CheckResult:
        rep = run_budget(root, params=params)
        s = rep.summary
        if s.core_deps > s.max_core_deps:
            return CheckResult(
                ok=False,
                message=f"Core dependencies ({s.core_deps}) exceed limit ({s.max_core_deps})",
                fix=f"Reduce runtime dependencies in pyproject.toml to <= {s.max_core_deps}",
            )
        if s.total_deps > s.max_total_deps:
            return CheckResult(
                ok=False,
                message=f"Total dependencies ({s.total_deps}) exceed limit ({s.max_total_deps})",
                fix=f"Reduce plugin and core dependencies to <= {s.max_total_deps}",
            )
        msg = (
            f"Dependencies within budget ({s.core_deps}/{s.max_core_deps} core, "
            f"{s.total_deps}/{s.max_total_deps} total)"
        )
        return CheckResult(ok=True, message=msg)

    def check_size() -> CheckResult:
        rep = run_budget(root, params=params)
        s = rep.summary
        advisories: list[str] = []
        if s.core_lines > s.core_lines_guidance:
            advisories.append(f"core lines {s.core_lines} > {s.core_lines_guidance}")
        if s.core_modules > s.core_modules_guidance:
            advisories.append(f"core modules {s.core_modules} > {s.core_modules_guidance}")
        for pl in rep.plugins:
            if pl.status != "ok":
                advisories.append(f"plugin {pl.name} {pl.status}")
        if advisories:
            return CheckResult(
                ok=False,
                message=f"Codebase size exceeds guidance: {', '.join(advisories)}",
                fix="Review complexity and consider refactoring or updating guidance parameters",
            )
        msg = f"Codebase size within guidance ({s.core_lines} lines, {s.core_modules} modules)"
        return CheckResult(ok=True, message=msg)

    return [
        DoctorCheck(id="budget.dependencies", severity=Severity.CRITICAL, run=check_deps),
        DoctorCheck(id="budget.size", severity=Severity.WARNING, run=check_size),
    ]
