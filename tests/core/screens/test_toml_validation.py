"""Tests for strict TOML screen validation and error messages naming file and key."""

from __future__ import annotations

from pathlib import Path

import pytest

from algodesk.api.errors import ConfigError
from algodesk.screens.models import load_toml_screen


def test_load_nonexistent_file_raises_config_error(tmp_path: Path) -> None:
    bad_path = tmp_path / "does_not_exist.toml"
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(bad_path)
    err = exc_info.value
    assert "not found" in err.what.lower()
    assert str(bad_path) in err.where


def test_load_syntax_error_raises_config_error(tmp_path: Path) -> None:
    bad_path = tmp_path / "syntax.toml"
    bad_path.write_text("name = 'bad\n[invalid toml", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(bad_path)
    err = exc_info.value
    assert "syntax" in err.what.lower()
    assert ":syntax" in err.where


def test_missing_name_raises_config_error(tmp_path: Path) -> None:
    p = tmp_path / "no_name.toml"
    p.write_text(
        "[[require]]\nleft = { field = 'close' }\nop = '>'\nright = 100\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert "name" in err.where or "name" in err.what


def test_empty_screen_raises_config_error(tmp_path: Path) -> None:
    p = tmp_path / "empty.toml"
    p.write_text("name = 'empty_screen'\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert "require" in err.what.lower() or "group" in err.what.lower()


def test_invalid_field_names_key_and_file(tmp_path: Path) -> None:
    p = tmp_path / "bad_field.toml"
    p.write_text(
        "name = 'bad_field'\n"
        "[[require]]\n"
        "left = { field = 'nonexistent_price' }\n"
        "op = '>'\n"
        "right = 100\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert str(p) in err.where
    assert "require" in err.where
    assert "nonexistent_price" in err.what


def test_invalid_op_names_key_and_file(tmp_path: Path) -> None:
    p = tmp_path / "bad_op.toml"
    p.write_text(
        "name = 'bad_op'\n"
        "[[require]]\n"
        "left = { field = 'close' }\n"
        "op = 'INVALID_OP'\n"
        "right = 100\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert str(p) in err.where
    assert "require" in err.where
    assert "INVALID_OP" in err.what


def test_between_without_low_high_raises_config_error(tmp_path: Path) -> None:
    p = tmp_path / "bad_between.toml"
    p.write_text(
        "name = 'bad_between'\n"
        "[[require]]\n"
        "left = { field = 'close' }\n"
        "op = 'between'\n"
        "right = 100\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert "between" in err.what.lower()
    assert str(p) in err.where


def test_crosses_above_within_less_than_1(tmp_path: Path) -> None:
    p = tmp_path / "bad_within.toml"
    p.write_text(
        "name = 'bad_within'\n"
        "[[require]]\n"
        "left = { field = 'close' }\n"
        "op = 'crosses_above'\n"
        "right = 100\n"
        "within = 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert "within" in err.what.lower() or "within" in err.where


def test_invalid_group_mode(tmp_path: Path) -> None:
    p = tmp_path / "bad_grp_mode.toml"
    p.write_text(
        "name = 'bad_grp'\n"
        "[[group]]\n"
        "mode = 'xor'\n"
        "[[group.require]]\n"
        "left = { field = 'close' }\n"
        "op = '>'\n"
        "right = 100\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert "xor" in err.what or "mode" in err.where


def test_group_at_least_without_n(tmp_path: Path) -> None:
    p = tmp_path / "at_least_no_n.toml"
    p.write_text(
        "name = 'at_least_no_n'\n"
        "[[group]]\n"
        "mode = 'at_least'\n"
        "[[group.require]]\n"
        "left = { field = 'close' }\n"
        "op = '>'\n"
        "right = 100\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert "n" in err.what.lower()


def test_group_at_least_n_exceeds_rules(tmp_path: Path) -> None:
    p = tmp_path / "at_least_too_big.toml"
    p.write_text(
        "name = 'at_least_too_big'\n"
        "[[group]]\n"
        "mode = 'at_least'\n"
        "n = 5\n"
        "[[group.require]]\n"
        "left = { field = 'close' }\n"
        "op = '>'\n"
        "right = 100\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert "exceeds" in err.what.lower()


def test_negative_bars_ago_raises(tmp_path: Path) -> None:
    p = tmp_path / "neg_bars_ago.toml"
    p.write_text(
        "name = 'neg_bars_ago'\n"
        "[[require]]\n"
        "left = { field = 'close', bars_ago = -1 }\n"
        "op = '>'\n"
        "right = 100\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert "bars_ago" in err.what.lower() or "bars_ago" in err.where


def test_invalid_adjust_mode(tmp_path: Path) -> None:
    p = tmp_path / "bad_adjust.toml"
    p.write_text(
        "name = 'bad_adjust'\n"
        "adjust = 'dividends_only'\n"
        "[[require]]\n"
        "left = { field = 'close' }\n"
        "op = '>'\n"
        "right = 100\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert "adjust" in err.where or "adjust" in err.what.lower()


def test_rank_min_out_of_bounds(tmp_path: Path) -> None:
    p = tmp_path / "bad_rank_min.toml"
    p.write_text(
        "name = 'bad_rank_min'\n"
        "[[require]]\n"
        "left = { field = 'close' }\n"
        "op = '>'\n"
        "right = 100\n"
        "[rank]\n"
        "by = { field = 'close' }\n"
        "min = 120.0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_toml_screen(p)
    err = exc_info.value
    assert "rank" in err.where or "120" in err.what


def test_valid_toml_screen_loads_successfully(tmp_path: Path) -> None:
    p = tmp_path / "valid.toml"
    p.write_text(
        "name = 'valid_screen'\n"
        "description = 'A completely valid test screen'\n"
        "timeframe = '1d'\n"
        "adjust = 'total'\n"
        "[[require]]\n"
        "left = { ind = 'sma', n = 50 }\n"
        "op = '>'\n"
        "right = { ind = 'sma', n = 200 }\n"
        "[rank]\n"
        "by = { ind = 'roc', n = 126 }\n"
        "method = 'percentile'\n"
        "min = 70.0\n",
        encoding="utf-8",
    )
    model = load_toml_screen(p)
    assert model.name == "valid_screen"
    assert model.timeframe == "1d"
    assert model.adjust == "total"
    assert len(model.require) == 1
    assert model.rank is not None
    assert model.rank.min == 70.0
