"""Tests for code hashing and conformance tracking."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def test_code_hash_deterministic(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.conformance import code_hash

    p_dir = fake_plugin_factory(tmp_path, "notifier", "hash_test")
    h1 = code_hash(p_dir)
    h2 = code_hash(p_dir)
    assert h1 == h2
    assert len(h1) == 64


def test_code_hash_changes_on_py_edit(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.conformance import code_hash

    p_dir = fake_plugin_factory(tmp_path, "notifier", "hash_edit")
    h1 = code_hash(p_dir)
    (p_dir / "plugin.py").write_text("# edited code\n", encoding="utf-8")
    h2 = code_hash(p_dir)
    assert h1 != h2


def test_code_hash_changes_on_toml_edit(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.conformance import code_hash

    p_dir = fake_plugin_factory(tmp_path, "notifier", "hash_toml")
    h1 = code_hash(p_dir)
    toml = (p_dir / "plugin.toml").read_text(encoding="utf-8")
    (p_dir / "plugin.toml").write_text(toml + "\n# comment\n", encoding="utf-8")
    h2 = code_hash(p_dir)
    assert h1 != h2


def test_code_hash_ignores_tests(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.conformance import code_hash

    p_dir = fake_plugin_factory(tmp_path, "notifier", "hash_tests")
    h1 = code_hash(p_dir)
    (p_dir / "tests" / "test_fake.py").write_text("# changed test\n", encoding="utf-8")
    (p_dir / "tests" / "another_test.py").write_text("def test_x(): pass\n", encoding="utf-8")
    h2 = code_hash(p_dir)
    assert h1 == h2


def test_code_hash_ignores_pycache(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.conformance import code_hash

    p_dir = fake_plugin_factory(tmp_path, "notifier", "hash_pycache")
    h1 = code_hash(p_dir)
    cache_dir = p_dir / "__pycache__"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "plugin.cpython-313.pyc").write_bytes(b"\x00\x01\x02")
    h2 = code_hash(p_dir)
    assert h1 == h2


def test_code_hash_ignores_readme(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.conformance import code_hash

    p_dir = fake_plugin_factory(tmp_path, "notifier", "hash_readme")
    h1 = code_hash(p_dir)
    (p_dir / "README.md").write_text("# completely different doc\n", encoding="utf-8")
    h2 = code_hash(p_dir)
    assert h1 == h2


def test_conformance_store_record_and_is_current(tmp_path: Path) -> None:
    from algodesk.api.kinds import PluginKind
    from algodesk.registry.conformance import ConformanceStore

    state_dir = tmp_path / ".algodesk"
    store = ConformanceStore(state_dir)
    assert store.is_current(PluginKind.NOTIFIER, "demo", "abc123hash") is False

    store.record_pass(PluginKind.NOTIFIER, "demo", "unit", "abc123hash")
    assert store.is_current(PluginKind.NOTIFIER, "demo", "abc123hash") is True
    assert store.is_current(PluginKind.NOTIFIER, "demo", "different_hash") is False
    assert (state_dir / "conformance.json").is_file()


def test_conformance_store_stale_after_hash_change(
    tmp_path: Path, fake_plugin_factory: Any
) -> None:
    from algodesk.registry.conformance import ConformanceStore, code_hash

    p_dir = fake_plugin_factory(tmp_path, "notifier", "stale_test")
    h1 = code_hash(p_dir)

    state_dir = tmp_path / ".algodesk"
    store = ConformanceStore(state_dir)
    store.record_pass("notifier", "stale_test", "suite1", h1)
    assert store.is_current("notifier", "stale_test", h1) is True

    # Modify code
    (p_dir / "plugin.py").write_text("# edited code\n", encoding="utf-8")
    h2 = code_hash(p_dir)
    assert store.is_current("notifier", "stale_test", h2) is False


def test_conformance_store_forget(tmp_path: Path) -> None:
    from algodesk.registry.conformance import ConformanceStore

    state_dir = tmp_path / ".algodesk"
    store = ConformanceStore(state_dir)
    store.record_pass("notifier", "p1", "suite", "hash1")
    store.record_pass("notifier", "p2", "suite", "hash2")

    store.forget("notifier", "p1")
    assert store.is_current("notifier", "p1", "hash1") is False
    assert store.is_current("notifier", "p2", "hash2") is True

    store.forget()
    assert store.is_current("notifier", "p2", "hash2") is False


def test_conformance_store_forget_invalid_args(tmp_path: Path) -> None:
    from algodesk.registry.conformance import ConformanceStore

    state_dir = tmp_path / ".algodesk"
    store = ConformanceStore(state_dir)
    with pytest.raises(ValueError, match="Both kind and name"):
        store.forget(kind="notifier")


def test_conformance_store_corrupt_file_raises_plugin_error(tmp_path: Path) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.registry.conformance import ConformanceStore

    state_dir = tmp_path / ".algodesk"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "conformance.json").write_text("{invalid json", encoding="utf-8")

    store = ConformanceStore(state_dir)
    with pytest.raises(PluginError) as exc_info:
        store.is_current("notifier", "p1", "hash1")
    assert "corrupt or unreadable" in exc_info.value.what
    assert exc_info.value.fix != ""


def test_conformance_store_not_dict_raises_plugin_error(tmp_path: Path) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.registry.conformance import ConformanceStore

    state_dir = tmp_path / ".algodesk"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "conformance.json").write_text('["a", "b"]', encoding="utf-8")

    store = ConformanceStore(state_dir)
    with pytest.raises(PluginError) as exc_info:
        store.is_current("notifier", "p1", "hash1")
    assert "corrupt or unreadable" in exc_info.value.what


def test_conformance_store_atomic_write_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from algodesk.registry.conformance import ConformanceStore

    state_dir = tmp_path / ".algodesk"
    store = ConformanceStore(state_dir)
    store.record_pass("notifier", "p1", "suite1", "hash1")
    assert store.is_current("notifier", "p1", "hash1") is True

    original_content = (state_dir / "conformance.json").read_text(encoding="utf-8")

    # Simulate failure during atomic rename
    def fail_replace(src: str, dst: str) -> None:
        raise OSError("Simulated disk error during rename")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="Simulated disk error"):
        store.record_pass("notifier", "p2", "suite2", "hash2")

    # Verify old file is intact
    assert (state_dir / "conformance.json").read_text(encoding="utf-8") == original_content
    # Verify no partial or temp files remain in state_dir
    files_in_state = list(state_dir.iterdir())
    assert files_in_state == [state_dir / "conformance.json"]
