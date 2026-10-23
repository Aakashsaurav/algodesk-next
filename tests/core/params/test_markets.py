"""Tests for market parameters in config/params/markets.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from algodesk.ops.paths import find_project_root
from algodesk.params.defaults import load_defaults
from algodesk.params.registry import ParamRegistry
from algodesk.params.specs import ParamSpec

MARKET_PARAMS = [
    # NSE EQ (7)
    ("market.nse.eq.preopen.open", "time", "09:00"),
    ("market.nse.eq.preopen.close", "time", "09:15"),
    ("market.nse.eq.regular.open", "time", "09:15"),
    ("market.nse.eq.regular.close", "time", "15:30"),
    ("market.nse.eq.weekdays", "str", "MON,TUE,WED,THU,FRI"),
    ("market.nse.eq.timezone", "str", "Asia/Kolkata"),
    ("market.nse.eq.squareoff", "time", "15:15"),
    # BSE EQ (7)
    ("market.bse.eq.preopen.open", "time", "09:00"),
    ("market.bse.eq.preopen.close", "time", "09:15"),
    ("market.bse.eq.regular.open", "time", "09:15"),
    ("market.bse.eq.regular.close", "time", "15:30"),
    ("market.bse.eq.weekdays", "str", "MON,TUE,WED,THU,FRI"),
    ("market.bse.eq.timezone", "str", "Asia/Kolkata"),
    ("market.bse.eq.squareoff", "time", "15:15"),
    # NSE FNO (5)
    ("market.nse.fno.regular.open", "time", "09:15"),
    ("market.nse.fno.regular.close", "time", "15:30"),
    ("market.nse.fno.weekdays", "str", "MON,TUE,WED,THU,FRI"),
    ("market.nse.fno.timezone", "str", "Asia/Kolkata"),
    ("market.nse.fno.squareoff", "time", "15:15"),
    # NSE CDS (5)
    ("market.nse.cds.regular.open", "time", "09:00"),
    ("market.nse.cds.regular.close", "time", "17:00"),
    ("market.nse.cds.weekdays", "str", "MON,TUE,WED,THU,FRI"),
    ("market.nse.cds.timezone", "str", "Asia/Kolkata"),
    ("market.nse.cds.squareoff", "time", "16:45"),
    # MCX COM (7)
    ("market.mcx.com.morning.open", "time", "09:00"),
    ("market.mcx.com.morning.close", "time", "17:00"),
    ("market.mcx.com.evening.open", "time", "17:00"),
    ("market.mcx.com.evening.close", "time", "23:30"),
    ("market.mcx.com.weekdays", "str", "MON,TUE,WED,THU,FRI"),
    ("market.mcx.com.timezone", "str", "Asia/Kolkata"),
    ("market.mcx.com.squareoff", "time", "23:15"),
]


@pytest.fixture(scope="module")
def market_specs() -> dict[str, ParamSpec]:
    root = find_project_root()
    specs = load_defaults(root / "config" / "params")
    return {s.name: s for s in specs if s.name.startswith("market.")}


def test_market_parameter_count(market_specs: dict[str, ParamSpec]) -> None:
    """Exactly 31 market parameters exist in defaults."""
    assert len(market_specs) == len(MARKET_PARAMS)


@pytest.mark.parametrize("name,expected_type,expected_default", MARKET_PARAMS)
def test_market_param_spec(
    market_specs: dict[str, ParamSpec],
    name: str,
    expected_type: str,
    expected_default: str,
) -> None:
    """Each market parameter has correct spec, type, default, and tighten rule."""
    spec = market_specs.get(name)
    assert spec is not None, f"Missing spec for {name}"
    assert spec.type == expected_type
    assert str(spec.default) == expected_default
    assert spec.tighten == "none"
    assert spec.owner == "core"
    assert len(spec.description) > 0


@pytest.mark.parametrize("name,expected_type,expected_default", MARKET_PARAMS)
def test_market_param_registry_lookup(
    tmp_path: Path,
    market_specs: dict[str, ParamSpec],
    name: str,
    expected_type: str,
    expected_default: str,
) -> None:
    """Each market parameter can be read through ParamRegistry."""
    from algodesk.api.kinds import Mode

    db_path = tmp_path / "test_params.db"
    registry = ParamRegistry(db_path, mode=Mode.PAPER)
    try:
        spec = market_specs[name]
        registry.register(spec)
        val = registry.get(name)
        assert str(val) == expected_default
    finally:
        registry.close()


@pytest.mark.parametrize("name,expected_type,expected_default", MARKET_PARAMS)
def test_market_param_override(
    tmp_path: Path,
    market_specs: dict[str, ParamSpec],
    name: str,
    expected_type: str,
    expected_default: str,
) -> None:
    """Each market parameter can be overridden in ParamRegistry."""
    from algodesk.api.kinds import Mode

    db_path = tmp_path / "test_override.db"
    registry = ParamRegistry(db_path, mode=Mode.PAPER)
    try:
        spec = market_specs[name]
        registry.register(spec)

        if expected_type == "time":
            new_val = "10:00"
        else:
            new_val = "MON,TUE"

        res = registry.set(name, new_val, reason="valid override reason", by="tester")
        assert res.status == "applied"
        assert str(registry.get(name)) == new_val
    finally:
        registry.close()
