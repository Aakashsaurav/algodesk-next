"""Tests for instrument groups loading and inspection."""

from __future__ import annotations

from pathlib import Path

import pytest

from algodesk.api.errors import ConfigError
from algodesk.data.groups import get_group, list_group_details, load_groups


def test_load_demo5_group() -> None:
    groups = load_groups("config/groups")
    assert "demo5" in groups
    assert "NSE:EQ:HDFCBANK" in groups["demo5"]
    assert "NSE:EQ:SBIN" in groups["demo5"]
    assert len(groups["demo5"]) == 5


def test_get_group_found_and_not_found() -> None:
    members = get_group("demo5", "config/groups")
    assert members is not None
    assert len(members) == 5

    missing = get_group("non_existent_group", "config/groups")
    assert missing is None


def test_list_group_details() -> None:
    details = list_group_details("config/groups")
    assert "demo5" in details
    info = details["demo5"]
    assert info["name"] == "demo5"
    assert "demo" in info["description"].lower()
    assert len(info["members"]) == 5


def test_load_groups_corrupt_file_raises(tmp_path: Path) -> None:
    corrupt = tmp_path / "bad.toml"
    corrupt.write_text("invalid = [toml unclosed", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_groups(tmp_path)
    assert "Failed to load group TOML file" in exc_info.value.what
