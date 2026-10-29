"""Tests for documentation accuracy against codebase implementation (DOC-1..DOC-10)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from typer.main import get_command

from algodesk.cli.app import app
from algodesk.ops.paths import find_project_root
from algodesk.params.defaults import load_defaults

ALLOWED_NON_PARAM_NAMES = {
    "algodesk.api": "Core plugin API import package",
    "algodesk.api.protocols": "Core protocols module",
    "requires.packages": "Plugin manifest requirement specification attribute",
    "config.valid": "Doctor diagnostic check ID for configuration validity",
    "env.isolated": "Doctor diagnostic check ID for workspace isolation",
    "plugins.conformance": "Doctor diagnostic check ID for plugin conformance",
    "check.sh": "Shell quality check script filename",
    "env.sh": "Environment setup script filename",
    "metrics.json": "Execution results metrics filename",
    "platform.toml": "Platform core configuration filename",
    "plugin.py": "Plugin module implementation filename",
    "plugin.toml": "Plugin manifest configuration filename",
    "uv.lock": "Dependency lockfile filename",
    "zerodha_nse.toml": "Example fee schedule filename",
    "data_manifest.db": "Data manifest database filename",
    "day.parquet": "Compacted day parquet filename",
    "order.created": "Journal event type for order creation",
    "config.effective.txt": "Effective configuration snapshot filename",
    "equity.csv": "Equity curve timeseries filename",
    "journal.db.lock": "Journal database lock filename",
    "manifest.json": "Execution run manifest filename",
    "registry.snapshot.json": "Parameter registry snapshot filename",
    "session.md": "Telegram session markdown summary filename",
    "trades.csv": "Executed trades CSV filename",
}

KNOWN_RUNTIME_PREFIXES = (
    "data/",
    "logs/",
    "results/",
    ".algodesk/",
    ".local/",
    ".venv/",
    "_workbench/",
)


def _gather_doc_files(root: Path) -> list[Path]:
    files = [root / "README.md"]
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        files.extend(sorted(docs_dir.glob("*.md")))
    return [f for f in files if f.is_file()]


def _extract_cli_command_tree(cli_root: Any) -> dict[str, set[str]]:
    tree: dict[str, set[str]] = {}

    def _walk(prefix: str, command: Any) -> None:
        if hasattr(command, "commands"):
            for sub_name, sub_cmd in command.commands.items():
                sub_prefix = f"{prefix} {sub_name}".strip()
                _walk(sub_prefix, sub_cmd)
        opts = {opt for param in command.params for opt in param.opts}
        if prefix:
            tree[prefix] = opts

    _walk("", cli_root)
    return tree


def test_every_documented_algodesk_command_exists_in_cli() -> None:
    """Step 6: Every algodesk command in code blocks or backticks exists in Typer CLI."""
    root = find_project_root()
    doc_files = _gather_doc_files(root)
    cli_root: Any = get_command(app)
    cli_tree = _extract_cli_command_tree(cli_root)
    root_global_opts = {opt for param in cli_root.params for opt in param.opts}

    command_pattern = re.compile(r"`(algodesk\s+[^`]+)`")
    extracted_commands: list[tuple[str, str]] = []

    for doc in doc_files:
        content = doc.read_text(encoding="utf-8")
        for match in command_pattern.finditer(content):
            extracted_commands.append((doc.name, match.group(1).strip()))

    assert len(extracted_commands) >= 20, "Expected at least 20 documented algodesk commands"

    for doc_name, full_cmd in extracted_commands:
        tokens = full_cmd.split()
        assert tokens[0] == "algodesk"
        cmd_tokens = tokens[1:]

        # Identify command path of arbitrary depth (e.g., 'doctor', 'param set', 'data trash purge')
        matched_cmd: str | None = None
        rest: list[str] = []
        for k in range(min(len(cmd_tokens), 4), 0, -1):
            candidate = " ".join(cmd_tokens[:k])
            if candidate in cli_tree:
                matched_cmd = candidate
                rest = cmd_tokens[k:]
                break

        assert matched_cmd is not None, (
            f"Document '{doc_name}' references unknown CLI command '{full_cmd}'. "
            f"Available: {sorted(cli_tree.keys())}"
        )

        # Validate options and flags
        cmd_opts = cli_tree[matched_cmd] | root_global_opts
        for token in rest:
            clean_token = token.strip("[](),\"'")
            if clean_token.startswith("-"):
                opt_name = clean_token.split("=")[0]
                assert opt_name in cmd_opts, (
                    f"Document '{doc_name}' references unknown flag '{opt_name}' "
                    f"in '{full_cmd}'. Expected one of {sorted(cmd_opts)}"
                )


def test_every_documented_parameter_name_exists_in_defaults_or_allow_list() -> None:
    """Step 7: Every dotted backtick identifier exists in shipped defaults or allow-list."""
    root = find_project_root()
    doc_files = _gather_doc_files(root)
    shipped_specs = load_defaults(root / "config" / "params")
    shipped_names = {s.name for s in shipped_specs}

    dotted_pattern = re.compile(r"`([a-z0-9_]+(?:\.[a-z0-9_]+)+)`")
    found_dotted: set[str] = set()

    for doc in doc_files:
        content = doc.read_text(encoding="utf-8")
        for match in dotted_pattern.finditer(content):
            found_dotted.add(match.group(1))

    assert len(found_dotted) > 0

    unresolved: dict[str, str] = {}
    for name in sorted(found_dotted):
        if name not in shipped_names and name not in ALLOWED_NON_PARAM_NAMES:
            unresolved[name] = "Not in shipped defaults and not in allow-list"

    assert not unresolved, f"Found unregistered dotted identifiers in docs: {unresolved}"


def test_every_documented_path_exists_or_is_known_runtime() -> None:
    """Step 8: Every backticked path in config/, src/, etc. exists or is valid runtime path."""
    root = find_project_root()
    doc_files = _gather_doc_files(root)
    repo_prefixes = ("config/", "src/", "scripts/", "tests/", "docs/", "plugins/")

    path_pattern = re.compile(r"`([^`]+)`")
    checked_repo_paths = 0
    checked_runtime_paths = 0

    for doc in doc_files:
        content = doc.read_text(encoding="utf-8")
        for match in path_pattern.finditer(content):
            token = match.group(1).strip()
            # Check repository code/config/doc paths
            for pfx in repo_prefixes:
                if token.startswith(pfx):
                    checked_repo_paths += 1
                    cleaned = token.rstrip("/").split("*")[0].split("<")[0]
                    target = root / cleaned
                    assert target.exists(), (
                        f"Document '{doc.name}' references non-existent path: '{token}' "
                        f"(checked '{cleaned}')"
                    )
                    break
            # Check runtime data/log/state paths
            for rpfx in KNOWN_RUNTIME_PREFIXES:
                if token.startswith(rpfx):
                    checked_runtime_paths += 1
                    break

    assert checked_repo_paths >= 8, f"Too few repo paths checked ({checked_repo_paths})"
    assert checked_runtime_paths >= 5, f"Too few runtime paths checked ({checked_runtime_paths})"


def test_documented_check_sh_steps_match_scripts_check_sh() -> None:
    """Step 9: Every documented check.sh step appears in scripts/check.sh."""
    root = find_project_root()
    check_sh = root / "scripts" / "check.sh"
    content = check_sh.read_text(encoding="utf-8")

    step_headers = re.findall(r"===\s+([^=]+)\s+===", content)
    assert len(step_headers) >= 8, "Expected at least 8 steps in check.sh"

    arch_doc = root / "docs" / "ARCHITECTURE.md"
    arch_content = arch_doc.read_text(encoding="utf-8")

    # Steps documented in ARCHITECTURE.md Section 7
    documented_steps = [
        ("isolation", ["isolation"]),
        ("ruff check", ["ruff check"]),
        ("ruff format check", ["ruff format"]),
        ("mypy", ["mypy"]),
        ("pytest", ["pytest"]),
        ("import-linter contracts", ["lint-imports", "import-linter"]),
        ("budget", ["budget"]),
        ("docs check", ["docs check"]),
        ("architecture lint", ["algodesk lint", "=== lint ==="]),
        ("secret scan", ["secrets scan", "secretscan"]),
    ]

    for doc_step, matching_patterns in documented_steps:
        # 1. Documented step is mentioned in docs/ARCHITECTURE.md
        keyword = doc_step.split()[0]
        assert keyword in arch_content, f"Documented step '{doc_step}' missing in ARCHITECTURE.md"

        # 2. Step appears in scripts/check.sh either as header or command
        matched = any(pattern in content for pattern in matching_patterns)
        assert matched, (
            f"Documented check.sh step '{doc_step}' does not appear in scripts/check.sh "
            f"(looked for {matching_patterns})"
        )


def test_docs_index_lists_exactly_all_docs_files() -> None:
    """Step 9: docs/INDEX.md lists exactly the files in docs/ (and exactly 7 files)."""
    root = find_project_root()
    docs_dir = root / "docs"
    actual_files = {f.name for f in docs_dir.glob("*.md")}

    expected_exact = {
        "ARCHITECTURE.md",
        "DECISIONS.md",
        "INDEX.md",
        "PLUGINS.md",
        "PRD.md",
        "RUNBOOK.md",
        "STATUS.md",
    }
    assert actual_files == expected_exact, (
        f"docs/ must contain exactly 7 files. Found: {sorted(actual_files)}"
    )

    index_text = (docs_dir / "INDEX.md").read_text(encoding="utf-8")
    for fname in expected_exact:
        if fname == "INDEX.md":
            continue
        assert f"[{fname}]({fname})" in index_text, (
            f"docs/INDEX.md does not list expected document [{fname}]({fname})"
        )
