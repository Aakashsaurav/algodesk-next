"""Plugin code hashing and conformance tracking."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from algodesk.api.errors import PluginError
from algodesk.api.kinds import PluginKind


def code_hash(plugin_dir: Path | str) -> str:
    """Compute sha256 over sorted relative paths and bytes of .py files and plugin.toml.

    Ignores tests/, __pycache__, and README.
    """
    path = Path(plugin_dir).resolve()
    hasher = hashlib.sha256()

    files_to_hash: list[tuple[str, Path]] = []
    for p in path.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(path)
        parts = rel.parts
        if "tests" in parts or "__pycache__" in parts:
            continue
        if any(part.startswith("README") for part in parts):
            continue
        if p.name == "plugin.toml" or p.suffix == ".py":
            files_to_hash.append((rel.as_posix(), p))

    files_to_hash.sort(key=lambda item: item[0])
    for rel_posix, file_path in files_to_hash:
        hasher.update(rel_posix.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(file_path.read_bytes())

    return hasher.hexdigest()


class ConformanceStore:
    """Tracks plugin conformance passes in state_dir/conformance.json."""

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir).resolve()
        self.file_path = self.state_dir / "conformance.json"

    def _read_records(self) -> dict[str, dict[str, Any]]:
        if not self.file_path.exists():
            return {}
        try:
            content = self.file_path.read_text(encoding="utf-8")
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("Expected top-level JSON object")
            return data
        except Exception as exc:
            raise PluginError(
                what=f"Conformance store is corrupt or unreadable: {exc}",
                where=str(self.file_path),
                fix="Delete or recreate conformance.json with valid JSON",
            ) from exc

    def _write_records(self, records: dict[str, dict[str, Any]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        content = json.dumps(records, indent=2, sort_keys=True)
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(self.state_dir),
            delete=False,
            encoding="utf-8",
        )
        try:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, str(self.file_path))
        except BaseException:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)
            raise

    def record_pass(
        self,
        kind: PluginKind | str,
        name: str,
        suite: str,
        hash: str,
    ) -> None:
        """Record a passing conformance test suite for a plugin."""
        k = kind.value if isinstance(kind, PluginKind) else str(kind)
        key = f"{k}/{name}"
        records = self._read_records()
        records[key] = {
            "kind": k,
            "name": name,
            "suite": suite,
            "hash": hash,
        }
        self._write_records(records)

    def is_current(
        self,
        kind: PluginKind | str,
        name: str,
        hash: str,
    ) -> bool:
        """Return True if the plugin has a record matching hash."""
        k = kind.value if isinstance(kind, PluginKind) else str(kind)
        key = f"{k}/{name}"
        records = self._read_records()
        rec = records.get(key)
        if rec is None:
            return False
        return bool(rec.get("hash") == hash)

    def forget(
        self,
        kind: PluginKind | str | None = None,
        name: str | None = None,
    ) -> None:
        """Remove a single record or clear all conformance records."""
        if kind is None and name is None:
            self._write_records({})
            return
        if kind is None or name is None:
            raise ValueError("Both kind and name must be provided to forget a specific plugin")
        k = kind.value if isinstance(kind, PluginKind) else str(kind)
        key = f"{k}/{name}"
        records = self._read_records()
        if key in records:
            del records[key]
            self._write_records(records)
