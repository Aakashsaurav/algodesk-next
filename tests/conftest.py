"""Pytest test suite configuration and global fixtures for AlgoDesk Next."""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex
_CURRENT_TEST_IS_LIVE = False


def _is_loopback(host: str) -> bool:
    clean_host = host.strip().lower()
    if clean_host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", ""):
        return True
    try:
        ip = ipaddress.ip_address(clean_host)
        return ip.is_loopback or ip.is_unspecified
    except ValueError:
        return False


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    if os.environ.get("ALGODESK_LIVE_TESTS") == "1" or _CURRENT_TEST_IS_LIVE:
        return _ORIGINAL_CONNECT(self, address)

    # Allow AF_UNIX sockets
    if getattr(self, "family", None) == getattr(socket, "AF_UNIX", -1):
        return _ORIGINAL_CONNECT(self, address)

    # Address is usually (host, port) or (host, port, flowinfo, scopeid)
    if isinstance(address, tuple) and len(address) >= 1:
        host = str(address[0])
        if _is_loopback(host):
            return _ORIGINAL_CONNECT(self, address)

    try:
        self.close()
    except Exception:
        pass
    raise RuntimeError(
        f"Network connection to {address} blocked during tests! "
        "Tests must not touch external network unless marked @pytest.mark.live "
        "or ALGODESK_LIVE_TESTS=1 is set."
    )


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if os.environ.get("ALGODESK_LIVE_TESTS") == "1" or _CURRENT_TEST_IS_LIVE:
        return _ORIGINAL_CONNECT_EX(self, address)

    if getattr(self, "family", None) == getattr(socket, "AF_UNIX", -1):
        return _ORIGINAL_CONNECT_EX(self, address)

    if isinstance(address, tuple) and len(address) >= 1:
        host = str(address[0])
        if _is_loopback(host):
            return _ORIGINAL_CONNECT_EX(self, address)

    try:
        self.close()
    except Exception:
        pass
    raise RuntimeError(
        f"Network connection (connect_ex) to {address} blocked during tests! "
        "Tests must not touch external network unless marked @pytest.mark.live "
        "or ALGODESK_LIVE_TESTS=1 is set."
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    socket.socket.connect = _guarded_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]


def pytest_runtest_setup(item: pytest.Item) -> None:
    global _CURRENT_TEST_IS_LIVE
    _CURRENT_TEST_IS_LIVE = (
        os.environ.get("ALGODESK_LIVE_TESTS") == "1" or item.get_closest_marker("live") is not None
    )


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    global _CURRENT_TEST_IS_LIVE
    _CURRENT_TEST_IS_LIVE = False


GUARDED_FOLDERS: tuple[str, ...] = ("data", "results", ".algodesk")
_BEFORE_TEST_SESSION_SNAPSHOT: set[str] | None = None


def snapshot_guarded_paths(root: Path) -> set[str]:
    """Capture relative paths of all files in guarded directories."""
    files: set[str] = set()
    for folder in GUARDED_FOLDERS:
        folder_path = root / folder
        if folder_path.is_dir():
            for p in folder_path.rglob("*"):
                name = p.name
                if (
                    name == ".DS_Store"
                    or "__pycache__" in p.as_posix()
                    or name.endswith(".pyc")
                    or name.endswith(".db-shm")
                    or name.endswith(".db-wal")
                ):
                    continue
                files.add(f"{folder}/{p.relative_to(folder_path).as_posix()}")
    return files


def verify_repo_state_snapshot(before: set[str], after: set[str]) -> None:
    """Compare before and after state snapshots and raise if files leaked."""
    added = after - before
    removed = before - after
    if added or removed:
        msg: list[str] = [
            "Hermetic test violation: repository state changed during test execution!"
        ]
        if added:
            msg.append(f"Added files ({len(added)}): " + ", ".join(sorted(added)[:10]))
        if removed:
            msg.append(f"Removed files ({len(removed)}): " + ", ".join(sorted(removed)[:10]))
        raise RuntimeError("\n".join(msg))


@pytest.fixture
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an isolated, temporary AlgoDesk workspace root for hermetic testing."""
    proj = tmp_path / "project"
    proj.mkdir(parents=True, exist_ok=True)
    real_root = Path(__file__).resolve().parent.parent

    # Copy pyproject.toml
    shutil.copyfile(real_root / "pyproject.toml", proj / "pyproject.toml")

    # Copy config directory
    if (real_root / "config").is_dir():
        shutil.copytree(real_root / "config", proj / "config")

    # Copy plugins directory (copying required to satisfy NFR-14 workspace root constraint)
    plugins_src = real_root / "plugins"
    if plugins_src.is_dir():
        shutil.copytree(plugins_src, proj / "plugins")

    # Symlink or copy src directory
    src_dir = real_root / "src"
    if src_dir.is_dir():
        try:
            os.symlink(src_dir, proj / "src")
        except OSError:
            shutil.copytree(src_dir, proj / "src")

    # Symlink or copy scripts directory
    scripts_dir = real_root / "scripts"
    if scripts_dir.is_dir():
        try:
            os.symlink(scripts_dir, proj / "scripts")
        except OSError:
            shutil.copytree(scripts_dir, proj / "scripts")

    # Ensure empty isolated state directories exist
    (proj / "data").mkdir(mode=0o700, parents=True, exist_ok=True)
    (proj / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    (proj / "results").mkdir(mode=0o700, parents=True, exist_ok=True)
    (proj / ".algodesk").mkdir(mode=0o700, parents=True, exist_ok=True)
    (proj / ".local").mkdir(mode=0o700, parents=True, exist_ok=True)
    (proj / "_workbench").mkdir(mode=0o700, parents=True, exist_ok=True)

    # Seed conformance records if available
    real_conf = real_root / ".algodesk" / "conformance.json"
    if real_conf.is_file():
        shutil.copyfile(real_conf, proj / ".algodesk" / "conformance.json")

    monkeypatch.chdir(proj)
    return proj


def pytest_sessionstart(session: pytest.Session) -> None:
    """Snapshot repository state before tests begin (controller only)."""
    global _BEFORE_TEST_SESSION_SNAPSHOT
    if hasattr(session.config, "workerinput"):
        return
    real_root = Path(__file__).resolve().parent.parent
    _BEFORE_TEST_SESSION_SNAPSHOT = snapshot_guarded_paths(real_root)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Verify repository state is unchanged after tests complete (controller only)."""
    global _BEFORE_TEST_SESSION_SNAPSHOT
    if hasattr(session.config, "workerinput"):
        return
    if _BEFORE_TEST_SESSION_SNAPSHOT is None:
        return
    real_root = Path(__file__).resolve().parent.parent
    after_snapshot = snapshot_guarded_paths(real_root)
    try:
        verify_repo_state_snapshot(_BEFORE_TEST_SESSION_SNAPSHOT, after_snapshot)
    except RuntimeError as exc:
        pytest.exit(f"Session Guard Failed: {exc}", returncode=1)
