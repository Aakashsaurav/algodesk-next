"""Workspace paths discovery and state file locations."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from algodesk.api.errors import ConfigError


def find_project_root(start: Path | str | None = None) -> Path:
    """Walk upward from start to find pyproject.toml where [project].name is 'algodesk'.

    Raises
    ------
    ConfigError
        If no matching project root is found before filesystem root.
    """
    current = Path(start).resolve() if start is not None else Path.cwd().resolve()
    candidate: Path | None = current
    while candidate is not None:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file():
            try:
                with open(pyproject, "rb") as f:
                    data = tomllib.load(f)
                if data.get("project", {}).get("name") == "algodesk":
                    return candidate
            except Exception:
                pass
        if candidate.parent == candidate:
            break
        candidate = candidate.parent

    raise ConfigError(
        what="Project root not found: no pyproject.toml with [project].name = 'algodesk'",
        where=str(current),
        fix="Run from within an AlgoDesk project or specify --root",
    )


@dataclass(frozen=True)
class StatePaths:
    """Paths to state databases and conformance records."""

    params_db: Path
    journal_db: Path
    conformance_dir: Path

    def __init__(
        self,
        *,
        params_db: Path | str,
        journal_db: Path | str,
        conformance_dir: Path | str,
    ) -> None:
        object.__setattr__(self, "params_db", Path(params_db))
        object.__setattr__(self, "journal_db", Path(journal_db))
        object.__setattr__(self, "conformance_dir", Path(conformance_dir))

    @classmethod
    def resolve(cls, root: Path | str, data_dir: Path | str) -> StatePaths:
        """Construct state paths from workspace root and data directory."""
        r = Path(root).resolve()
        d = Path(data_dir).resolve()
        return cls(
            params_db=d / "state" / "params.db",
            journal_db=d / "state" / "journal.db",
            conformance_dir=r / ".algodesk",
        )
