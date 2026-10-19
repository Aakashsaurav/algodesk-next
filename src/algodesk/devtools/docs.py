"""Documentation consistency, summary rules, and index generator (DOC-1..DOC-10)."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from algodesk.api.checks import CheckResult, DoctorCheck, Severity

_SUMMARY_HEADING_REGEX = re.compile(r"^(?:#|##)\s+Summary\b", re.IGNORECASE)
_HEADING_REGEX = re.compile(r"^#+\s+")
_MARKDOWN_LINK_REGEX = re.compile(r"(?<!\!)\[(?P<text>[^\]]*)\]\((?P<url>[^)]+)\)")
_OWNER_REGEX = re.compile(r"(?i)(?:owner)\s*[:=]")
_VERSION_REGEX = re.compile(r"(?i)(?:version)\s*[:=]")


@dataclass(frozen=True)
class DocFinding:
    """An individual finding from docs check."""

    path: str
    line: int
    rule: str
    message: str
    is_warning: bool = False

    def format(self) -> str:
        return f"{self.path}:{self.line} {self.rule} {self.message}"


@dataclass(frozen=True)
class DocsReport:
    """Report summarizing docs check execution."""

    checked: int
    findings: list[DocFinding]
    errors: list[DocFinding]
    warnings: list[DocFinding]
    exit_code: int


def load_docs_params(root: Path, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Load documentation limits and guidance from registry or core.toml."""
    res: dict[str, Any] = {}
    keys = ["docs.max_summary_words", "docs.max_living_docs"]
    if params is not None:
        for k in keys:
            if k in params:
                res[k] = params[k]
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
    return res


def extract_summary(content: str) -> tuple[int, str]:
    """Extract line number and text of Summary section within the first 40 lines."""
    lines = content.splitlines()
    limit = min(40, len(lines))
    summary_line = 0
    for idx in range(limit):
        if _SUMMARY_HEADING_REGEX.match(lines[idx].strip()):
            summary_line = idx + 1
            break

    if summary_line == 0:
        return 0, ""

    body_lines: list[str] = []
    for line in lines[summary_line:]:
        if _HEADING_REGEX.match(line.strip()):
            break
        body_lines.append(line)

    return summary_line, "\n".join(body_lines).strip()


def _check_summary(rel_path: str, content: str, max_words: int, findings: list[DocFinding]) -> None:
    line_num, text = extract_summary(content)
    if line_num == 0:
        findings.append(
            DocFinding(
                path=rel_path,
                line=1,
                rule="docs.summary_heading",
                message="Missing Summary heading (## Summary or # Summary) within first 40 lines",
            )
        )
        return

    words = text.split()
    if not words:
        findings.append(
            DocFinding(
                path=rel_path,
                line=line_num,
                rule="docs.summary_empty",
                message="Summary section is empty; must contain 1 to {max_words} words",
            )
        )
    elif len(words) > max_words:
        findings.append(
            DocFinding(
                path=rel_path,
                line=line_num,
                rule="docs.summary_length",
                message=f"Summary section has {len(words)} words, exceeds maximum of {max_words}",
            )
        )


def _check_relative_links(
    file_path: Path, rel_path: str, content: str, findings: list[DocFinding]
) -> None:
    for line_idx, line in enumerate(content.splitlines(), start=1):
        for m in _MARKDOWN_LINK_REGEX.finditer(line):
            url = m.group("url").strip()
            if url.startswith(("#", "http://", "https://", "mailto:", "ftp:")):
                continue
            file_part = url.split("#", 1)[0].strip()
            if not file_part:
                continue
            target = (file_path.parent / file_part).resolve()
            if not target.exists():
                findings.append(
                    DocFinding(
                        path=rel_path,
                        line=line_idx,
                        rule="docs.broken_link",
                        message=f"Relative link '{url}' does not resolve to an existing path",
                    )
                )


def _check_metadata(rel_path: str, content: str, findings: list[DocFinding]) -> None:
    if not rel_path.startswith("docs/"):
        return
    has_owner = bool(_OWNER_REGEX.search(content))
    has_version = bool(_VERSION_REGEX.search(content))
    if not (has_owner and has_version):
        findings.append(
            DocFinding(
                path=rel_path,
                line=1,
                rule="docs.metadata_missing",
                message="Document under docs/ lacks Owner or Version metadata",
                is_warning=True,
            )
        )


def gather_doc_files(root: Path) -> list[Path]:
    """Collect README.md, AGENTS.md, docs/**/*.md and plugins/**/README.md."""
    doc_files: set[Path] = set()
    r = root / "README.md"
    if r.is_file():
        doc_files.add(r)
    a = root / "AGENTS.md"
    if a.is_file():
        doc_files.add(a)

    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for f in docs_dir.rglob("*.md"):
            if "_workbench" not in f.parts and f.is_file():
                doc_files.add(f)

    plugins_dir = root / "plugins"
    if plugins_dir.is_dir():
        for f in plugins_dir.rglob("README.md"):
            if "_workbench" not in f.parts and f.is_file():
                doc_files.add(f)

    return sorted(doc_files)


def generate_index_content(root: Path) -> str:
    """Generate deterministic markdown content for docs/INDEX.md."""
    docs_dir = root / "docs"
    items: list[tuple[str, str]] = []
    if docs_dir.is_dir():
        for f in sorted(docs_dir.rglob("*.md")):
            if f.name == "INDEX.md" or not f.is_file():
                continue
            rel = f.relative_to(docs_dir).as_posix()
            try:
                cnt = f.read_text(encoding="utf-8")
                _, smm = extract_summary(cnt)
                clean_smm = " ".join(smm.split())
            except Exception:
                clean_smm = ""
            items.append((rel, clean_smm))

    lines = [
        "## Summary",
        "",
        "Auto-generated living documentation index for AlgoDesk Next.",
        "",
        "<!-- Owner: core | Version: 0.0.0 -->",
        "<!-- generated, do not edit -->",
        "",
        "## Documents",
        "",
        "| Document | Summary |",
        "|---|---|",
    ]
    for rel_path, smm in items:
        lines.append(f"| [{rel_path}]({rel_path}) | {smm} |")
    lines.append("")
    return "\n".join(lines)


def write_index(root: Path) -> Path:
    """Write regenerated docs/INDEX.md deterministically."""
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    idx_path = docs_dir / "INDEX.md"
    content = generate_index_content(root)
    idx_path.write_text(content, encoding="utf-8")
    return idx_path


def run_docs_check(root: Path, params: Mapping[str, Any] | None = None) -> DocsReport:
    """Execute docs check suite across markdown documentation."""
    dp = load_docs_params(root, params)
    max_summary_words = int(dp.get("docs.max_summary_words", 150))
    max_living_docs = int(dp.get("docs.max_living_docs", 12))

    findings: list[DocFinding] = []
    files = gather_doc_files(root)

    for f in files:
        rel = f.relative_to(root).as_posix()
        try:
            content = f.read_text(encoding="utf-8")
        except Exception as exc:
            findings.append(DocFinding(path=rel, line=1, rule="docs.read_error", message=str(exc)))
            continue

        _check_summary(rel, content, max_summary_words, findings)
        _check_relative_links(f, rel, content, findings)
        _check_metadata(rel, content, findings)

    docs_dir = root / "docs"
    if docs_dir.is_dir():
        living_docs = [p for p in docs_dir.rglob("*.md") if p.is_file()]
        if len(living_docs) > max_living_docs:
            findings.append(
                DocFinding(
                    path="docs",
                    line=1,
                    rule="docs.living_docs_limit",
                    message=(
                        f"Found {len(living_docs)} files under docs/, "
                        f"exceeds limit of {max_living_docs}"
                    ),
                )
            )

        idx_file = docs_dir / "INDEX.md"
        status_file = docs_dir / "STATUS.md"
        if not idx_file.is_file():
            findings.append(
                DocFinding(
                    path="docs/INDEX.md",
                    line=1,
                    rule="docs.required_file",
                    message="Required file docs/INDEX.md does not exist",
                )
            )
        else:
            expected = generate_index_content(root)
            actual = idx_file.read_text(encoding="utf-8")
            if actual != expected:
                findings.append(
                    DocFinding(
                        path="docs/INDEX.md",
                        line=1,
                        rule="docs.index_stale",
                        message="docs/INDEX.md is stale, run 'algodesk docs index' to regenerate",
                    )
                )

        if not status_file.is_file():
            findings.append(
                DocFinding(
                    path="docs/STATUS.md",
                    line=1,
                    rule="docs.required_file",
                    message="Required file docs/STATUS.md does not exist",
                )
            )
        else:
            st_text = status_file.read_text(encoding="utf-8")
            st_words = len(st_text.split())
            if st_words > 400:
                findings.append(
                    DocFinding(
                        path="docs/STATUS.md",
                        line=1,
                        rule="docs.status_length",
                        message=f"docs/STATUS.md has {st_words} words, exceeds maximum of 400",
                    )
                )

    errors = [f for f in findings if not f.is_warning]
    warnings = [f for f in findings if f.is_warning]
    exit_code = 1 if errors else 0
    return DocsReport(
        checked=len(files),
        findings=findings,
        errors=errors,
        warnings=warnings,
        exit_code=exit_code,
    )


def format_docs_text(report: DocsReport) -> str:
    """Format docs report for CLI output."""
    first_line = (
        f"DOCS: {report.checked} documents checked, "
        f"{len(report.errors)} errors, {len(report.warnings)} warnings"
    )
    lines = [first_line]
    for finding in report.findings:
        lines.append(finding.format())
    return "\n".join(lines)


def make_docs_check(runtime: Any) -> DoctorCheck:
    """Create doctor check for documentation rules (Severity.WARNING)."""
    root = Path(runtime.root)

    def check() -> CheckResult:
        rep = run_docs_check(root)
        if rep.errors:
            return CheckResult(
                ok=False,
                message=f"Docs check failed with {len(rep.errors)} error(s)",
                fix="Run 'algodesk docs check' and fix documentation rule violations",
            )
        if rep.warnings:
            msg = f"Docs valid with {len(rep.warnings)} warning(s) ({rep.checked} docs checked)"
            return CheckResult(ok=True, message=msg)
        return CheckResult(
            ok=True,
            message=f"Documentation valid ({report_count} documents checked)"
            if (report_count := rep.checked)
            else "Documentation valid",
        )

    return DoctorCheck(id="docs.rules", severity=Severity.WARNING, run=check)
