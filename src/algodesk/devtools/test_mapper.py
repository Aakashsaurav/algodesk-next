"""Maps changed source and plugin files to test targets for fast test loops."""

from __future__ import annotations

import subprocess
from pathlib import Path


def get_git_diff_files(root: Path | None = None) -> list[str]:
    """Retrieve list of modified and unstaged/staged files via git diff."""
    base_dir = root or Path.cwd()
    files: set[str] = set()
    try:
        # Check both working tree diff and staged diff
        out_unstaged = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=base_dir,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        for line in out_unstaged.splitlines():
            if line.strip():
                files.add(line.strip())

        out_staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=base_dir,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        for line in out_staged.splitlines():
            if line.strip():
                files.add(line.strip())
    except Exception:
        pass
    return sorted(files)


def map_path_to_tests(rel_path: str, root: Path | None = None) -> list[str]:
    """Map a single source or plugin path to its corresponding test file or directory."""
    base_dir = root or Path.cwd()
    norm = rel_path.replace("\\", "/").strip()
    if not norm:
        return []

    # If it's already a test path
    if norm.startswith("tests/"):
        target = base_dir / norm
        if target.exists():
            return [norm]
        return []

    # If it's under plugins/K/N
    if norm.startswith("plugins/"):
        parts = norm.split("/")
        if len(parts) >= 3:
            plugin_dir = base_dir / parts[0] / parts[1] / parts[2]
            plugin_tests = plugin_dir / "tests"
            if plugin_tests.is_dir():
                return [f"{parts[0]}/{parts[1]}/{parts[2]}/tests"]
        return []

    # If it's under src/algodesk/X
    if norm.startswith("src/algodesk/"):
        sub = norm[len("src/algodesk/") :]
        parts = sub.split("/")
        pkg = parts[0]
        filename = parts[-1] if len(parts) > 1 else ""

        # Try specific test file: tests/core/<pkg>/test_<stem>.py
        if filename.endswith(".py"):
            stem = filename[:-3]
            specific_test = base_dir / "tests" / "core" / pkg / f"test_{stem}.py"
            if specific_test.is_file():
                return [f"tests/core/{pkg}/test_{stem}.py"]

        # Try package test directory: tests/core/<pkg>
        pkg_test_dir = base_dir / "tests" / "core" / pkg
        if pkg_test_dir.is_dir():
            return [f"tests/core/{pkg}"]

        # Try direct match under tests/core/<sub_path>
        direct_test = base_dir / "tests" / "core" / sub
        if direct_test.exists():
            return [f"tests/core/{sub}"]

    return []


def resolve_test_targets(paths: list[str] | None = None, root: Path | None = None) -> list[str]:
    """Resolve input paths or git diff files into a deduplicated list of test targets."""
    base_dir = root or Path.cwd()
    inputs = paths if paths is not None else get_git_diff_files(base_dir)

    targets: list[str] = []
    seen: set[str] = set()

    for p in inputs:
        resolved = map_path_to_tests(p, root=base_dir)
        for t in resolved:
            if t not in seen:
                seen.add(t)
                targets.append(t)

    # Fallback to tests/core if no specific target mapped
    if not targets:
        return ["tests/core"]
    return targets


def main() -> None:
    import sys

    raw_args = sys.argv[1:]
    targets = resolve_test_targets(raw_args)
    print(" ".join(targets))


if __name__ == "__main__":
    main()
