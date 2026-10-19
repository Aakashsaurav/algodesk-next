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


@dataclass(frozen=True, init=False)
class StatePaths:
    """Paths to state databases and conformance records."""

    params_db: Path
    journal_db: Path
    conformance_dir: Path

    def __init__(
        self,
        params_db_or_root: Path | str | None = None,
        journal_db_or_data: Path | str | None = None,
        conformance_dir: Path | str | None = None,
        *,
        params_db: Path | str | None = None,
        journal_db: Path | str | None = None,
        root: Path | str | None = None,
        data_dir: Path | str | None = None,
    ) -> None:
        if data_dir is not None:
            d = Path(data_dir).resolve()
            r = Path(root).resolve() if root is not None else d.parent
            p_db = Path(params_db) if params_db else d / "state" / "params.db"
            j_db = Path(journal_db) if journal_db else d / "state" / "journal.db"
            c_dir = Path(conformance_dir) if conformance_dir else r / ".algodesk"
        elif conformance_dir is not None:
            p_db = (
                Path(params_db_or_root)
                if params_db_or_root
                else (Path(params_db) if params_db else Path())
            )
            j_db = (
                Path(journal_db_or_data)
                if journal_db_or_data
                else (Path(journal_db) if journal_db else Path())
            )
            c_dir = Path(conformance_dir)
        elif params_db is not None or journal_db is not None:
            p_db = Path(params_db) if params_db else Path()
            j_db = Path(journal_db) if journal_db else Path()
            c_dir = Path(conformance_dir) if conformance_dir else Path()
        elif params_db_or_root is not None and journal_db_or_data is not None:
            r = Path(params_db_or_root).resolve()
            d = Path(journal_db_or_data).resolve()
            p_db = d / "state" / "params.db"
            j_db = d / "state" / "journal.db"
            c_dir = r / ".algodesk"
        else:
            p_db = Path(params_db_or_root) if params_db_or_root else Path()
            j_db = Path(journal_db_or_data) if journal_db_or_data else Path()
            c_dir = Path(conformance_dir) if conformance_dir else Path()

        object.__setattr__(self, "params_db", p_db)
        object.__setattr__(self, "journal_db", j_db)
        object.__setattr__(self, "conformance_dir", c_dir)

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
