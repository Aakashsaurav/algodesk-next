"""Architecture AST linter for safety, stubs, limits, imports, and duplicates."""

from __future__ import annotations

import ast
import json
import sys
import tomllib
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from algodesk.api.checks import CheckResult, DoctorCheck, Severity


@dataclass(frozen=True)
class LintFinding:
    """An individual finding from architecture linting."""

    path: str
    line: int
    rule: str
    message: str
    symbol: str = ""
    is_warning: bool = False

    def format(self) -> str:
        return f"{self.path}:{self.line} {self.rule} {self.message}"


@dataclass(frozen=True)
class ArchlintConfig:
    """Configured rules and thresholds for archlint."""

    banned_functions: tuple[str, ...]
    ban_shell_true: bool
    allowed_docstring_exceptions: bool
    return_true_prefixes: tuple[str, ...]
    limit_patterns: tuple[str, ...]
    ignore_numeric_values: tuple[int | float, ...]
    allowed_framework_roots: tuple[str, ...]


@dataclass(frozen=True)
class ArchlintReport:
    """Overall report from architecture linting."""

    checked: int
    findings: list[LintFinding]
    errors: list[LintFinding]
    warnings: list[LintFinding]
    exit_code: int


def load_archlint_config(root: Path) -> ArchlintConfig:
    """Load config/archlint.toml rules and patterns."""
    cfg_file = root / "config" / "archlint.toml"
    banned_fns = ("eval", "exec", "os.system")
    ban_shell = True
    docstring_exc = True
    ret_true = ("check", "is_", "is", "verify", "ping")
    patterns = (
        "limit",
        "max_",
        "min_",
        "threshold",
        "timeout",
        "buffer",
        "retries",
        "ttl",
        "interval",
        "cap",
    )
    ignore_vals: tuple[int | float, ...] = (0, 1, -1, 0.0, 1.0, -1.0)
    allowed_fw = ("algodesk.api",)

    if cfg_file.is_file():
        try:
            data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
            bc = data.get("banned_calls", {})
            if "banned_functions" in bc:
                banned_fns = tuple(bc["banned_functions"])
            if "ban_shell_true" in bc:
                ban_shell = bool(bc["ban_shell_true"])

            sd = data.get("stub_detector", {})
            if "allowed_docstring_exceptions" in sd:
                docstring_exc = bool(sd["allowed_docstring_exceptions"])
            if "return_true_prefixes" in sd:
                ret_true = tuple(sd["return_true_prefixes"])

            hl = data.get("hard_coded_limits", {})
            if "patterns" in hl:
                patterns = tuple(hl["patterns"])
            if "ignore_values" in hl:
                ignore_vals = tuple(hl["ignore_values"])

            pi = data.get("plugin_imports", {})
            if "allowed_framework_roots" in pi:
                allowed_fw = tuple(pi["allowed_framework_roots"])
        except Exception:
            pass

    return ArchlintConfig(
        banned_functions=banned_fns,
        ban_shell_true=ban_shell,
        allowed_docstring_exceptions=docstring_exc,
        return_true_prefixes=ret_true,
        limit_patterns=patterns,
        ignore_numeric_values=ignore_vals,
        allowed_framework_roots=allowed_fw,
    )


def load_allow_list(root: Path) -> tuple[list[dict[str, Any]], list[LintFinding]]:
    """Load config/archlint_allow.toml entries and check for empty reasons."""
    allow_file = root / "config" / "archlint_allow.toml"
    entries: list[dict[str, Any]] = []
    findings: list[LintFinding] = []
    if not allow_file.is_file():
        return entries, findings

    try:
        data = tomllib.loads(allow_file.read_text(encoding="utf-8"))
        raw_list = data.get("allow", [])
        for idx, item in enumerate(raw_list):
            reason = str(item.get("reason", "")).strip()
            if not reason:
                msg = f"Allow-list entry for '{item.get('file', '')}' has an empty reason"
                findings.append(
                    LintFinding(
                        path="config/archlint_allow.toml",
                        line=idx + 1,
                        rule="archlint.empty_reason",
                        message=msg,
                    )
                )
            entries.append(
                {
                    "file": str(item.get("file", "")),
                    "rule": str(item.get("rule", "")),
                    "symbol": str(item.get("symbol", "")),
                    "reason": reason,
                    "used": False,
                }
            )
    except Exception as exc:
        findings.append(
            LintFinding(
                path="config/archlint_allow.toml",
                line=1,
                rule="archlint.allow_file_error",
                message=f"Failed to parse allow list: {exc}",
            )
        )
    return entries, findings


def _is_numeric_literal(node: ast.AST, ignore_vals: tuple[int | float, ...]) -> bool:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value not in ignore_vals
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        if isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float)):
            val = -node.operand.value if isinstance(node.op, ast.USub) else node.operand.value
            return val not in ignore_vals
    return False


def _matches_limit_pattern(name: str, patterns: tuple[str, ...]) -> bool:
    nl = name.lower()
    return any(p.lower() in nl for p in patterns)


class _AstChecker(ast.NodeVisitor):
    def __init__(
        self,
        rel_path: str,
        cfg: ArchlintConfig,
        is_plugin: bool,
        plugin_packages: set[str],
    ) -> None:
        self.rel_path = rel_path
        self.cfg = cfg
        self.is_plugin = is_plugin
        self.plugin_packages = plugin_packages
        self.findings: list[LintFinding] = []
        self._class_stack: list[ast.ClassDef] = []
        self._check_limits = not (
            "defaults.py" in rel_path
            or rel_path.startswith("config/params")
            or "tests/" in rel_path
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node)
        self.generic_visit(node)
        self._class_stack.pop()

    def _is_enclosing_protocol(self) -> bool:
        if not self._class_stack:
            return False
        cur = self._class_stack[-1]
        for base in cur.bases:
            if isinstance(base, ast.Name) and base.id == "Protocol":
                return True
            if isinstance(base, ast.Attribute) and base.attr == "Protocol":
                return True
        return False

    def _is_enclosing_exception(self) -> bool:
        if not self._class_stack:
            return False
        cur = self._class_stack[-1]
        if cur.name.endswith(("Error", "Exception")):
            return True
        for base in cur.bases:
            if isinstance(base, ast.Name) and base.id in (
                "Exception",
                "BaseException",
                "AlgoDeskError",
            ):
                return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._inspect_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._inspect_function(node)
        self.generic_visit(node)

    def _inspect_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Check hard-coded limits in parameter defaults
        if self._check_limits:
            all_args = list(node.args.args)
            defaults = list(node.args.defaults)
            offset = len(all_args) - len(defaults)
            for i, d in enumerate(defaults):
                arg = all_args[offset + i]
                if _matches_limit_pattern(arg.arg, self.cfg.limit_patterns):
                    if _is_numeric_literal(d, self.cfg.ignore_numeric_values):
                        self.findings.append(
                            LintFinding(
                                path=self.rel_path,
                                line=node.lineno,
                                rule="archlint.hard_coded_limit",
                                symbol=arg.arg,
                                message=f"Hard-coded limit in parameter default '{arg.arg}'",
                            )
                        )
            for kw_arg, kw_d in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=False):
                if kw_d and _matches_limit_pattern(kw_arg.arg, self.cfg.limit_patterns):
                    if _is_numeric_literal(kw_d, self.cfg.ignore_numeric_values):
                        self.findings.append(
                            LintFinding(
                                path=self.rel_path,
                                line=node.lineno,
                                rule="archlint.hard_coded_limit",
                                symbol=kw_arg.arg,
                                message=f"Hard-coded limit in keyword default '{kw_arg.arg}'",
                            )
                        )

        # Check stub detector
        if self._is_enclosing_protocol():
            return
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name) and dec.id in ("abstractmethod", "overload"):
                return
            if isinstance(dec, ast.Attribute) and dec.attr in ("abstractmethod", "overload"):
                return

        body_stmts = node.body
        if body_stmts and isinstance(body_stmts[0], ast.Expr):
            if isinstance(body_stmts[0].value, ast.Constant) and isinstance(
                body_stmts[0].value.value, str
            ):
                body_stmts = body_stmts[1:]

        if not body_stmts:
            if not (self.cfg.allowed_docstring_exceptions and self._is_enclosing_exception()):
                self.findings.append(
                    LintFinding(
                        path=self.rel_path,
                        line=node.lineno,
                        rule="archlint.stub_detector",
                        symbol=node.name,
                        message=f"Function '{node.name}' has no implementation (docstring only)",
                    )
                )
            return

        if len(body_stmts) == 1:
            stmt = body_stmts[0]
            if isinstance(stmt, ast.Pass):
                if not (self.cfg.allowed_docstring_exceptions and self._is_enclosing_exception()):
                    self.findings.append(
                        LintFinding(
                            path=self.rel_path,
                            line=node.lineno,
                            rule="archlint.stub_detector",
                            symbol=node.name,
                            message=f"Function '{node.name}' body is only 'pass'",
                        )
                    )
                return
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                if stmt.value.value is Ellipsis:
                    self.findings.append(
                        LintFinding(
                            path=self.rel_path,
                            line=node.lineno,
                            rule="archlint.stub_detector",
                            symbol=node.name,
                            message=f"Function '{node.name}' body is only '...'",
                        )
                    )
                    return
            if any(node.name.startswith(pfx) for pfx in self.cfg.return_true_prefixes):
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant):
                    if stmt.value.value is True:
                        self.findings.append(
                            LintFinding(
                                path=self.rel_path,
                                line=node.lineno,
                                rule="archlint.stub_detector",
                                symbol=node.name,
                                message=f"Function '{node.name}' is trivial stub returning True",
                            )
                        )

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._check_limits:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if _matches_limit_pattern(target.id, self.cfg.limit_patterns):
                        if _is_numeric_literal(node.value, self.cfg.ignore_numeric_values):
                            self.findings.append(
                                LintFinding(
                                    path=self.rel_path,
                                    line=node.lineno,
                                    rule="archlint.hard_coded_limit",
                                    symbol=target.id,
                                    message=f"Hard-coded limit assigned to variable '{target.id}'",
                                )
                            )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self._check_limits and isinstance(node.target, ast.Name) and node.value:
            if _matches_limit_pattern(node.target.id, self.cfg.limit_patterns):
                if _is_numeric_literal(node.value, self.cfg.ignore_numeric_values):
                    self.findings.append(
                        LintFinding(
                            path=self.rel_path,
                            line=node.lineno,
                            rule="archlint.hard_coded_limit",
                            symbol=node.target.id,
                            message=f"Hard-coded limit in annotation assignment '{node.target.id}'",
                        )
                    )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            func_name = f"{node.func.value.id}.{node.func.attr}"

        if func_name in self.cfg.banned_functions:
            self.findings.append(
                LintFinding(
                    path=self.rel_path,
                    line=node.lineno,
                    rule="archlint.banned_call",
                    symbol=func_name,
                    message=f"Call to '{func_name}' is banned",
                )
            )

        if self.cfg.ban_shell_true:
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant):
                    if kw.value.value is True:
                        self.findings.append(
                            LintFinding(
                                path=self.rel_path,
                                line=node.lineno,
                                rule="archlint.banned_call",
                                symbol="shell=True",
                                message="Call with 'shell=True' is banned",
                            )
                        )

        if self._check_limits:
            for kw in node.keywords:
                if kw.arg and _matches_limit_pattern(kw.arg, self.cfg.limit_patterns):
                    if _is_numeric_literal(kw.value, self.cfg.ignore_numeric_values):
                        self.findings.append(
                            LintFinding(
                                path=self.rel_path,
                                line=node.lineno,
                                rule="archlint.hard_coded_limit",
                                symbol=kw.arg,
                                message=f"Hard-coded limit in keyword argument '{kw.arg}'",
                            )
                        )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if self.is_plugin:
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                if root_pkg in sys.stdlib_module_names:
                    continue
                if alias.name == "algodesk.api" or alias.name.startswith("algodesk.api."):
                    continue
                if root_pkg in self.plugin_packages:
                    continue
                self.findings.append(
                    LintFinding(
                        path=self.rel_path,
                        line=node.lineno,
                        rule="archlint.plugin_import",
                        symbol=alias.name,
                        message=f"Plugin imported unauthorized package '{alias.name}'",
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self.is_plugin:
            if node.level and node.level > 0:
                self.generic_visit(node)
                return
            mod = node.module or ""
            root_pkg = mod.split(".")[0]
            if root_pkg in sys.stdlib_module_names:
                self.generic_visit(node)
                return
            if mod == "algodesk.api" or mod.startswith("algodesk.api."):
                self.generic_visit(node)
                return
            if root_pkg in self.plugin_packages:
                self.generic_visit(node)
                return
            self.findings.append(
                LintFinding(
                    path=self.rel_path,
                    line=node.lineno,
                    rule="archlint.plugin_import",
                    symbol=mod,
                    message=f"Plugin imported unauthorized module '{mod}'",
                )
            )
        self.generic_visit(node)


def _check_top_level_duplicate_classes(
    files: list[tuple[Path, str, ast.Module]], findings: list[LintFinding]
) -> None:
    class_map: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for _, rel_path, tree in files:
        if not rel_path.startswith("src/algodesk"):
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                class_map[node.name].append((rel_path, node.lineno))

    for cls_name, locations in class_map.items():
        if len(locations) > 1:
            loc_desc = ", ".join(f"{p}:{ln}" for p, ln in locations)
            msg = (
                f"Duplicate top-level class '{cls_name}' defined across multiple files: {loc_desc}"
            )
            for rel_path, lineno in locations[1:]:
                findings.append(
                    LintFinding(
                        path=rel_path,
                        line=lineno,
                        rule="archlint.duplicate_class",
                        symbol=cls_name,
                        message=msg,
                    )
                )


def _apply_allow_list(
    findings: list[LintFinding], allow_entries: list[dict[str, Any]]
) -> tuple[list[LintFinding], list[LintFinding]]:
    active_findings: list[LintFinding] = []
    for f in findings:
        matched = False
        for entry in allow_entries:
            file_match = f.path.endswith(entry["file"]) or entry["file"] in f.path
            rule_match = entry["rule"] in f.rule or f.rule in entry["rule"]
            symbol_match = not entry["symbol"] or entry["symbol"] == f.symbol
            if file_match and rule_match and symbol_match:
                entry["used"] = True
                matched = True
                break
        if not matched:
            active_findings.append(f)

    # Check for stale entries (no rot)
    stale_findings: list[LintFinding] = []
    for entry in allow_entries:
        if not entry["used"] and entry["reason"]:
            stale_msg = f"Stale allow-list entry: {entry['file']} {entry['rule']} {entry['symbol']}"
            stale_findings.append(
                LintFinding(
                    path="config/archlint_allow.toml",
                    line=1,
                    rule="archlint.stale_allow_entry",
                    symbol=entry["symbol"],
                    message=stale_msg,
                )
            )

    return active_findings, stale_findings


def _find_plugin_manifest_packages(file_path: Path, root: Path) -> set[str]:
    cur = file_path.parent
    while cur != root and cur != cur.parent:
        mf = cur / "plugin.toml"
        if mf.is_file():
            try:
                data = tomllib.loads(mf.read_text(encoding="utf-8"))
                pkgs = data.get("requires", {}).get("packages", [])
                return {
                    p.split(">")[0].split("<")[0].split("=")[0].strip().replace("-", "_")
                    for p in pkgs
                    if isinstance(p, str)
                }
            except Exception:
                return set()
        cur = cur.parent
    return set()


def run_archlint(root: Path) -> ArchlintReport:
    """Run full AST architecture linting over src/algodesk and plugins."""
    cfg = load_archlint_config(root)
    allow_entries, initial_findings = load_allow_list(root)

    parsed_files: list[tuple[Path, str, ast.Module]] = []
    findings: list[LintFinding] = list(initial_findings)

    src_dir = root / "src" / "algodesk"
    all_paths: list[Path] = []
    if src_dir.is_dir():
        all_paths.extend(p for p in src_dir.rglob("*.py") if "__pycache__" not in p.parts)

    plugins_dir = root / "plugins"
    if plugins_dir.is_dir():
        all_paths.extend(p for p in plugins_dir.rglob("*.py") if "__pycache__" not in p.parts)

    all_paths.sort()

    for p in all_paths:
        rel = p.relative_to(root).as_posix()
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            if isinstance(tree, ast.Module):
                parsed_files.append((p, rel, tree))
        except SyntaxError as exc:
            findings.append(
                LintFinding(
                    path=rel,
                    line=exc.lineno or 1,
                    rule="archlint.syntax_error",
                    message=str(exc),
                )
            )

    for p, rel, tree in parsed_files:
        is_plugin = rel.startswith("plugins/")
        plugin_pkgs = _find_plugin_manifest_packages(p, root) if is_plugin else set()
        checker = _AstChecker(rel, cfg, is_plugin, plugin_pkgs)
        checker.visit(tree)
        findings.extend(checker.findings)

    _check_top_level_duplicate_classes(parsed_files, findings)

    active_findings, stale_findings = _apply_allow_list(findings, allow_entries)
    active_findings.extend(stale_findings)

    errors = [f for f in active_findings if not f.is_warning]
    warnings = [f for f in active_findings if f.is_warning]
    exit_code = 1 if errors else 0

    return ArchlintReport(
        checked=len(all_paths),
        findings=active_findings,
        errors=errors,
        warnings=warnings,
        exit_code=exit_code,
    )


def format_archlint_text(report: ArchlintReport) -> str:
    """Format archlint report as required CLI text output."""
    first_line = (
        f"LINT: {report.checked} files checked, "
        f"{len(report.errors)} errors, {len(report.warnings)} warnings"
    )
    lines = [first_line]
    for finding in report.findings:
        lines.append(finding.format())
    return "\n".join(lines)


def format_archlint_json(report: ArchlintReport) -> str:
    """Format archlint report as JSON."""
    data = {
        "summary": {
            "checked": report.checked,
            "errors": len(report.errors),
            "warnings": len(report.warnings),
            "exit_code": report.exit_code,
        },
        "findings": [asdict(f) for f in report.findings],
    }
    return json.dumps(data, indent=2)


def make_archlint_check(runtime: Any) -> DoctorCheck:
    """Create doctor check for architecture linting (Severity.CRITICAL)."""
    root = Path(runtime.root)

    def check() -> CheckResult:
        rep = run_archlint(root)
        if rep.errors:
            fix_msg = (
                "Run 'algodesk lint' and fix reported architecture violations "
                "or allow-list with reason"
            )
            return CheckResult(
                ok=False,
                message=f"Architecture lint failed with {len(rep.errors)} error(s)",
                fix=fix_msg,
            )
        return CheckResult(
            ok=True,
            message=f"Architecture rules clean ({rep.checked} files checked)",
        )

    return DoctorCheck(id="lint.architecture", severity=Severity.CRITICAL, run=check)
