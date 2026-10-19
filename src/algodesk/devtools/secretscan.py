"""Secret token and credential scanner (INV-9, NFR-14)."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from algodesk.api.checks import CheckResult, DoctorCheck, Severity

_DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".local",
    "data",
    "logs",
    "results",
    "_workbench",
    "__pycache__",
}
_DEFAULT_LOCK_PATTERNS = {"*.lock", "uv.lock", "poetry.lock", "package-lock.json", "Cargo.lock"}


@dataclass(frozen=True)
class SecretFinding:
    """An individual secret scan finding without sensitive values."""

    path: str
    line: int
    rule: str
    message: str

    def format(self) -> str:
        return f"{self.path}:{self.line} {self.rule} {self.message}"


@dataclass(frozen=True)
class SecretScanConfig:
    """Rules and exclusions loaded from config/secretscan.toml."""

    private_key_re: re.Pattern[str]
    cloud_access_key_re: re.Pattern[str]
    github_token_re: re.Pattern[str]
    telegram_token_re: re.Pattern[str]
    quoted_secret_re: re.Pattern[str]
    placeholder_keywords: tuple[str, ...]
    excluded_dirs: set[str]
    lock_patterns: set[str]


@dataclass(frozen=True)
class SecretScanReport:
    """Overall report from secret scanning."""

    scanned: int
    findings: list[SecretFinding]
    exit_code: int


def load_secretscan_config(root: Path) -> SecretScanConfig:
    """Load patterns and exclusions from config/secretscan.toml."""
    cfg_file = root / "config" / "secretscan.toml"
    pk_str = r"-----BEGIN (?:[A-Z0-9_-]+ )?PRIVATE KEY-----"
    ak_str = r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    gh_str = r"\b(?:(?:ghp|gho|ghu|ghs|ghr)_[0-9a-zA-Z]{36}|github_pat_[0-9a-zA-Z_]{22,})\b"
    tg_str = r"\b\d{8,10}:[a-zA-Z0-9_-]{35}\b"
    qs_str = (
        r"(?i)\b(token|secret|password|passwd|api_key|apikey|credential)\b\s*[:=]\s*[\"']"
        r"([^\"']{16,})[\"']"
    )
    ph_keys = (
        "placeholder",
        "example",
        "dummy",
        "sample",
        "changeme",
        "your_",
        "default",
        "xxxx",
        "<",
        ">",
        "env:",
        "secret:",
        "fake-secret",
    )
    ex_dirs = set(_DEFAULT_EXCLUDED_DIRS)
    lock_pats = set(_DEFAULT_LOCK_PATTERNS)

    if cfg_file.is_file():
        try:
            data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
            pats = data.get("patterns", {})
            pk_str = pats.get("private_key", pk_str)
            ak_str = pats.get("cloud_access_key", ak_str)
            gh_str = pats.get("github_token", gh_str)
            tg_str = pats.get("telegram_token", tg_str)
            qs_str = pats.get("quoted_secret_regex", qs_str)
            ph = data.get("placeholders", {})
            if "keywords" in ph:
                ph_keys = tuple(ph["keywords"])
            ex = data.get("exclusions", {})
            if "directories" in ex:
                ex_dirs = set(ex["directories"])
            if "file_patterns" in ex:
                lock_pats = set(ex["file_patterns"])
        except Exception:
            pass

    return SecretScanConfig(
        private_key_re=re.compile(pk_str),
        cloud_access_key_re=re.compile(ak_str),
        github_token_re=re.compile(gh_str),
        telegram_token_re=re.compile(tg_str),
        quoted_secret_re=re.compile(qs_str),
        placeholder_keywords=ph_keys,
        excluded_dirs=ex_dirs,
        lock_patterns=lock_pats,
    )


def load_secretscan_allow(root: Path) -> dict[str, str]:
    """Load config/secretscan_allow.toml mapping allowed paths to reasons."""
    allow_file = root / "config" / "secretscan_allow.toml"
    res: dict[str, str] = {}
    if not allow_file.is_file():
        return res
    try:
        data = tomllib.loads(allow_file.read_text(encoding="utf-8"))
        for entry in data.get("allow", []):
            path_val = str(entry.get("path", "")).strip()
            reason = str(entry.get("reason", "")).strip()
            if path_val and reason:
                res[path_val] = reason
    except Exception:
        pass
    return res


def _is_lock_or_binary_file(p: Path, lock_patterns: set[str]) -> bool:
    name = p.name
    if any(p.match(pat) for pat in lock_patterns) or name.endswith(".lock"):
        return True
    try:
        with open(p, "rb") as f:
            chunk = f.read(8192)
            if b"\x00" in chunk:
                return True
    except OSError:
        return True
    return False


def _is_placeholder_or_secret_ref(val: str, keywords: tuple[str, ...]) -> bool:
    if val.startswith("secret:"):
        return True
    val_lower = val.lower()
    if any(k.lower() in val_lower for k in keywords):
        return True
    if len(set(val)) <= 2:
        return True
    if val.isupper() and "_" in val:
        return True
    return False


def _scan_line_for_secrets(
    rel: str, line_idx: int, line: str, cfg: SecretScanConfig
) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    if "# fake-secret" in line or "#fake-secret" in line:
        return findings

    if cfg.private_key_re.search(line):
        findings.append(
            SecretFinding(
                path=rel,
                line=line_idx,
                rule="secrets.private_key",
                message="Private key block detected",
            )
        )

    if cfg.cloud_access_key_re.search(line):
        findings.append(
            SecretFinding(
                path=rel,
                line=line_idx,
                rule="secrets.cloud_access_key",
                message="Cloud access key ID detected",
            )
        )

    if cfg.github_token_re.search(line):
        findings.append(
            SecretFinding(
                path=rel,
                line=line_idx,
                rule="secrets.github_token",
                message="GitHub token pattern detected",
            )
        )

    if cfg.telegram_token_re.search(line):
        findings.append(
            SecretFinding(
                path=rel,
                line=line_idx,
                rule="secrets.telegram_token",
                message="Telegram bot token pattern detected",
            )
        )

    for m in cfg.quoted_secret_re.finditer(line):
        val = m.group(2)
        if not _is_placeholder_or_secret_ref(val, cfg.placeholder_keywords):
            findings.append(
                SecretFinding(
                    path=rel,
                    line=line_idx,
                    rule="secrets.quoted_secret",
                    message="Quoted secret token assigned to credential key",
                )
            )
    return findings


def gather_files_to_scan(root: Path, cfg: SecretScanConfig) -> list[Path]:
    """Gather all project files outside excluded dirs and lock files."""
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts
        if any(part in cfg.excluded_dirs for part in rel_parts):
            continue
        if _is_lock_or_binary_file(p, cfg.lock_patterns):
            continue
        files.append(p)
    return sorted(files)


def run_secretscan(root: Path) -> SecretScanReport:
    """Scan project files for raw secrets and exposed credentials."""
    cfg = load_secretscan_config(root)
    allow_map = load_secretscan_allow(root)
    files = gather_files_to_scan(root, cfg)
    findings: list[SecretFinding] = []

    for f in files:
        rel = f.relative_to(root).as_posix()
        if rel in allow_map or any(rel.endswith(al) for al in allow_map):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for line_idx, line in enumerate(content.splitlines(), start=1):
            line_findings = _scan_line_for_secrets(rel, line_idx, line, cfg)
            findings.extend(line_findings)

    exit_code = 1 if findings else 0
    return SecretScanReport(scanned=len(files), findings=findings, exit_code=exit_code)


def format_secretscan_text(report: SecretScanReport) -> str:
    """Format secret scan output. Never prints the secret value."""
    first_line = f"SECRETS: {report.scanned} files scanned, {len(report.findings)} findings"
    lines = [first_line]
    for finding in report.findings:
        lines.append(finding.format())
    return "\n".join(lines)


def format_secretscan_json(report: SecretScanReport) -> str:
    """Format secret scan output as JSON without secret values."""
    data = {
        "summary": {
            "scanned": report.scanned,
            "findings": len(report.findings),
            "exit_code": report.exit_code,
        },
        "findings": [asdict(f) for f in report.findings],
    }
    return json.dumps(data, indent=2)


def make_secretscan_check(runtime: Any) -> DoctorCheck:
    """Create doctor diagnostic check for secrets (Severity.CRITICAL)."""
    root = Path(runtime.root)

    def check() -> CheckResult:
        rep = run_secretscan(root)
        if rep.findings:
            fix_msg = (
                "Remove exposed secrets; use secret:<name> references or allow-list with reason"
            )
            return CheckResult(
                ok=False,
                message=f"Secret scan found {len(rep.findings)} exposed credential(s)",
                fix=fix_msg,
            )
        return CheckResult(
            ok=True,
            message=f"No secrets found ({rep.scanned} files scanned)",
        )

    return DoctorCheck(id="secrets.scan", severity=Severity.CRITICAL, run=check)
