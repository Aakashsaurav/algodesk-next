"""Tests for parameter defaults loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_load_shipped_defaults() -> None:
    """Loading config/params must return all 25 core and risk parameters."""
    from algodesk.params.defaults import load_defaults
    from algodesk.params.specs import ParamType, Tighten

    specs = load_defaults(Path("config/params"))
    names = [s.name for s in specs]
    assert len(names) == len(set(names)), "Duplicate parameter names found in shipped defaults"
    required_names = {
        "risk.conservative.daily_loss_limit_pct",
        "risk.conservative.margin_buffer_pct",
        "ops.min_free_disk_gb",
        "budget.core_lines_guidance",
    }
    missing_required = required_names - set(names)
    assert not missing_required, f"Required parameter names missing: {missing_required}"

    by_name = {s.name: s for s in specs}

    # Verify risk parameters
    assert "risk.conservative.daily_loss_limit_pct" in by_name
    daily_loss = by_name["risk.conservative.daily_loss_limit_pct"]
    assert daily_loss.default == 1.5
    assert daily_loss.tighten == Tighten.DOWN
    assert daily_loss.type == ParamType.FLOAT

    margin_buf = by_name["risk.conservative.margin_buffer_pct"]
    assert margin_buf.default == 25.0
    assert margin_buf.tighten == Tighten.UP

    # Verify core parameters
    disk = by_name["ops.min_free_disk_gb"]
    assert disk.default == 5
    assert disk.tighten == Tighten.UP

    budget_lines = by_name["budget.core_lines_guidance"]
    assert budget_lines.default == 6000
    assert budget_lines.tighten == Tighten.DOWN


def test_reject_duplicate_parameter(tmp_path: Path) -> None:
    """Duplicate parameter name across toml files must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.defaults import load_defaults

    file1 = tmp_path / "a.toml"
    file1.write_text("""
[[param]]
name = "dup.test.limit"
type = "int"
default = 1
""")
    file2 = tmp_path / "b.toml"
    file2.write_text("""
[[param]]
name = "dup.test.limit"
type = "int"
default = 2
""")
    with pytest.raises(ParamError) as exc_info:
        load_defaults(tmp_path)
    assert "Duplicate parameter name" in exc_info.value.what


def test_reject_unknown_key(tmp_path: Path) -> None:
    """Unknown attribute in [[param]] table must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.defaults import load_defaults

    file1 = tmp_path / "test.toml"
    file1.write_text("""
[[param]]
name = "unknown.key.test"
type = "int"
default = 1
extra_invalid_key = 123
""")
    with pytest.raises(ParamError) as exc_info:
        load_defaults(tmp_path)
    assert "Unknown parameter attribute" in exc_info.value.what
    assert "extra_invalid_key" in exc_info.value.what


def test_reject_unknown_top_level_section(tmp_path: Path) -> None:
    """Non-param top level sections in TOML must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.defaults import load_defaults

    file1 = tmp_path / "test.toml"
    file1.write_text("""
[general]
mode = "test"

[[param]]
name = "valid.spec.name"
type = "int"
default = 1
""")
    with pytest.raises(ParamError) as exc_info:
        load_defaults(tmp_path)
    assert "Unknown top-level sections" in exc_info.value.what


def test_missing_directory_raises() -> None:
    """Non-existent directory must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.defaults import load_defaults

    with pytest.raises(ParamError) as exc_info:
        load_defaults(Path("/non/existent/path/for/defaults"))
    assert "Defaults directory not found" in exc_info.value.what


def test_invalid_table_format(tmp_path: Path) -> None:
    """Invalid param table structure must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.defaults import load_defaults

    file1 = tmp_path / "bad.toml"
    file1.write_text("param = 123")
    with pytest.raises(ParamError) as exc_info:
        load_defaults(tmp_path)
    assert "Expected [[param]] list of tables" in exc_info.value.what
