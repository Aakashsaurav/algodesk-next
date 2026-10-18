"""Tests for plugin discovery and issue collection."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def test_discover_valid_plugin(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    fake_plugin_factory(tmp_path, "notifier", "valid_plugin")
    plugins, issues = discover([tmp_path])
    assert len(issues) == 0
    assert len(plugins) == 1
    info = plugins[0]
    assert info.name == "valid_plugin"
    assert info.kind.value == "notifier"
    assert info.directory == tmp_path / "notifier" / "valid_plugin"
    assert len(info.code_hash) == 64


def test_discover_skips_underscore_folders(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    fake_plugin_factory(tmp_path, "_hidden_kind", "demo1")
    fake_plugin_factory(tmp_path, "notifier", "_hidden_name")
    fake_plugin_factory(tmp_path, "notifier", "visible_demo")

    plugins, issues = discover([tmp_path])
    assert len(issues) == 0
    assert len(plugins) == 1
    assert plugins[0].name == "visible_demo"


def test_discover_skips_non_directory_files(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    (tmp_path / "notifier").mkdir(parents=True, exist_ok=True)
    (tmp_path / "notifier" / "stray_file.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "stray.txt").write_text("hello", encoding="utf-8")

    fake_plugin_factory(tmp_path, "notifier", "demo")
    plugins, issues = discover([tmp_path])
    assert len(issues) == 0
    assert len(plugins) == 1


def test_discover_multiple_plugin_dirs(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    dir1 = tmp_path / "plugins1"
    dir2 = tmp_path / "plugins2"
    fake_plugin_factory(dir1, "notifier", "demo1")
    fake_plugin_factory(dir2, "venue", "demo2")

    plugins, issues = discover([dir1, dir2])
    assert len(issues) == 0
    assert len(plugins) == 2
    names = {p.name for p in plugins}
    assert names == {"demo1", "demo2"}


def test_discover_missing_plugin_toml(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    p_dir = tmp_path / "notifier" / "no_toml"
    p_dir.mkdir(parents=True, exist_ok=True)
    (p_dir / "README.md").write_text("# no_toml\n\n## Summary\nSummary\n", encoding="utf-8")
    (p_dir / "tests").mkdir(parents=True, exist_ok=True)
    (p_dir / "plugin.py").write_text("class Demo: pass\n", encoding="utf-8")

    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any("Missing plugin.toml" in issue.what for issue in issues)


def test_discover_bad_toml_syntax(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    p_dir = tmp_path / "notifier" / "bad_toml"
    p_dir.mkdir(parents=True, exist_ok=True)
    (p_dir / "plugin.toml").write_text("name = [unclosed string\n", encoding="utf-8")
    (p_dir / "README.md").write_text("# bad\n\n## Summary\nSummary\n", encoding="utf-8")
    (p_dir / "tests").mkdir(parents=True, exist_ok=True)

    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any("Bad TOML" in issue.what for issue in issues)


def test_discover_invalid_manifest_fields(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    fake_plugin_factory(tmp_path, "notifier", "bad_ver", version="invalid-semver")
    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any("Invalid manifest" in issue.what for issue in issues)


def test_discover_folder_name_mismatch(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    p_dir = fake_plugin_factory(tmp_path, "notifier", "name_a")
    renamed = p_dir.parent / "name_b"
    p_dir.rename(renamed)

    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any(
        "Folder name 'name_b' does not match manifest name 'name_a'" in i.what for i in issues
    )


def test_discover_folder_kind_mismatch(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    fake_plugin_factory(tmp_path, "notifier", "mismatched_kind")
    # Move notifier/mismatched_kind to venue/mismatched_kind without changing plugin.toml
    venue_dir = tmp_path / "venue"
    venue_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "notifier" / "mismatched_kind").rename(venue_dir / "mismatched_kind")

    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any(
        "Folder kind 'venue' does not match manifest kind 'notifier'" in i.what for i in issues
    )


def test_discover_duplicate_kind_and_name(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    fake_plugin_factory(dir1, "notifier", "dup_plugin")
    fake_plugin_factory(dir2, "notifier", "dup_plugin")

    plugins, issues = discover([dir1, dir2])
    assert len(plugins) == 1
    assert any("Duplicate plugin" in issue.what for issue in issues)


def test_discover_missing_readme(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    fake_plugin_factory(tmp_path, "notifier", "no_readme", readme_text="__OMIT__")
    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any("Missing README.md" in issue.what for issue in issues)


def test_discover_empty_readme(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    fake_plugin_factory(tmp_path, "notifier", "empty_readme", readme_text="")
    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any("README.md is empty" in issue.what for issue in issues)


def test_discover_whitespace_readme(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    fake_plugin_factory(tmp_path, "notifier", "ws_readme", readme_text="   \n\n\t  ")
    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any("README.md is empty" in issue.what for issue in issues)


def test_discover_readme_missing_summary_heading(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    text = "# Title\n\nSome introductory notes without a summary heading.\n"
    fake_plugin_factory(tmp_path, "notifier", "no_sum_head", readme_text=text)
    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any("missing a 'Summary' heading" in issue.what for issue in issues)


def test_discover_readme_summary_beyond_line_40(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    padding = "\n".join(f"Line {i}" for i in range(45))
    text = f"# Title\n{padding}\n## Summary\nLate summary\n"
    fake_plugin_factory(tmp_path, "notifier", "late_summary", readme_text=text)
    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any(
        "missing a 'Summary' heading in its first 40 lines" in issue.what for issue in issues
    )


def test_discover_readme_summary_within_line_40(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    padding = "\n".join(f"Line {i}" for i in range(25))
    text = f"# Title\n{padding}\n## Summary\nValid summary\n"
    fake_plugin_factory(tmp_path, "notifier", "ok_summary", readme_text=text)
    plugins, issues = discover([tmp_path])
    assert len(issues) == 0
    assert len(plugins) == 1


def test_discover_missing_tests_folder(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    fake_plugin_factory(tmp_path, "notifier", "no_tests", create_tests=False)
    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any("Missing tests/ folder" in issue.what for issue in issues)


def test_discover_entry_module_missing(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    fake_plugin_factory(
        tmp_path,
        "notifier",
        "missing_entry",
        entry="custom_entry:CustomClass",
        plugin_py_content="__OMIT__",
    )
    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any("Entry module file missing" in issue.what for issue in issues)


def test_discover_config_schema_missing(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    fake_plugin_factory(
        tmp_path,
        "notifier",
        "missing_schema",
        config_schema="schema.py",
    )
    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    assert any("Config schema file missing" in issue.what for issue in issues)


def test_discover_multi_problem_collection(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    p_dir = tmp_path / "notifier" / "multi_bad"
    p_dir.mkdir(parents=True, exist_ok=True)
    # Manifest says name is different, tests folder missing, readme missing, entry file missing
    manifest = (
        'name = "different_name"\n'
        'kind = "notifier"\n'
        'version = "0.1.0"\n'
        "api_version = 1\n"
        'entry = "missing_mod:MyClass"\n'
        'config_schema = "nonexistent_schema.py"\n'
        "[capabilities]\n"
        "send = true\n"
    )
    (p_dir / "plugin.toml").write_text(manifest, encoding="utf-8")

    plugins, issues = discover([tmp_path])
    assert len(plugins) == 0
    # Must collect folder name mismatch, missing readme, missing tests,
    # missing entry, and missing schema.
    whats = [i.what for i in issues]
    assert any("Folder name 'multi_bad' does not match manifest name" in w for w in whats)
    assert any("Missing README.md" in w for w in whats)
    assert any("Missing tests/ folder" in w for w in whats)
    assert any("Entry module file missing" in w for w in whats)
    assert any("Config schema file missing" in w for w in whats)
    assert len(issues) >= 5


def test_discover_imports_nothing(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.registry.discovery import discover

    # Put a poisonous module that crashes on import
    poison = "raise RuntimeError('SENTINEL: import should never happen during discovery!')\n"
    fake_plugin_factory(
        tmp_path,
        "notifier",
        "poison_plugin",
        plugin_py_content=poison,
    )

    plugins, issues = discover([tmp_path])
    assert len(issues) == 0
    assert len(plugins) == 1
    assert plugins[0].name == "poison_plugin"
