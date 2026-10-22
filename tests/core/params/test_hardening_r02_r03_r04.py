"""Tests for parameter hardening: R-02, R-03, R-04."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from algodesk.api.checks import Severity
from algodesk.api.errors import ParamError
from algodesk.ops.checks import _check_params_db
from algodesk.params.registry import ParamRegistry
from algodesk.params.specs import ParamSpec, ParamType, Tighten


def _make_spec(
    name: str,
    default: int = 100,
    min_val: int | None = 0,
    max_val: int | None = 500,
    scope: str = "global",
) -> ParamSpec:
    return ParamSpec(
        name=name,
        type=ParamType.INT,
        default=default,
        min=min_val,
        max=max_val,
        tighten=Tighten.NONE,
        scope=scope,
        description=f"Test spec for {name}",
    )


def test_r02_history_failure_leaves_value_and_seq_unchanged(tmp_path: Path) -> None:
    """R-02: Failure in history insert rolls back value and sequence updates atomically."""
    db_path = tmp_path / "atomic_params.db"
    reg = ParamRegistry(db_path)
    spec = _make_spec("risk.max_loss", default=50)
    reg.register(spec)
    seq0 = reg._store.get_change_seq()

    # Force history insert failure
    with patch.object(reg._store, "add_history", side_effect=RuntimeError("History disk full")):
        with pytest.raises(RuntimeError, match="History disk full"):
            reg.set("risk.max_loss", 80, by="user", reason="test history rollback")

    # Value and sequence must be completely unchanged
    assert reg.get("risk.max_loss") == 50
    assert reg._store.get_change_seq() == seq0
    raw_vals = reg._store.load_all_values()
    assert ("risk.max_loss", "global") not in raw_vals


def test_r03_changed_default_reloads_cleanly_and_valid_override_survives(tmp_path: Path) -> None:
    """R-03: Shipped defaults are authoritative; reloading updated default does not raise."""
    db_path = tmp_path / "reload_params.db"
    reg1 = ParamRegistry(db_path)
    spec1 = _make_spec("budget.lines", default=6000, min_val=0, max_val=50000)
    reg1.register(spec1)
    reg1.set("budget.lines", 12000, by="user", reason="valid override setting")
    reg1.close()

    # Reopen on same database
    reg2 = ParamRegistry(db_path)
    assert reg2.get("budget.lines") == 12000

    # New shipped default (e.g. 20000)
    defaults_dir = tmp_path / "defaults"
    defaults_dir.mkdir(parents=True, exist_ok=True)

    toml_content = """
[[param]]
name = "budget.lines"
type = "int"
default = 20000
min = 0
max = 50000
tighten = "none"
scope = "global"
description = "Updated lines guidance"
"""
    (defaults_dir / "core.toml").write_text(toml_content.strip())

    # load_defaults should reload cleanly without raising "Conflicting specification"
    reg2.load_defaults(defaults_dir)
    assert reg2.spec("budget.lines").default == 20000
    # The valid override (12000) must survive
    assert reg2.get("budget.lines") == 12000


def test_r03_out_of_bounds_override_invalidated(tmp_path: Path) -> None:
    """R-03: Override violating new bounds is invalidated with history row and doctor warning."""
    db_path = tmp_path / "inval_params.db"
    reg1 = ParamRegistry(db_path)
    spec1 = _make_spec("risk.limit", default=20, min_val=0, max_val=100)
    reg1.register(spec1)
    reg1.set("risk.limit", 80, by="user", reason="setting high limit")
    reg1.close()

    # Reopen and load defaults with narrower bounds (max 50, so 80 is invalid!)
    reg2 = ParamRegistry(db_path)
    defaults_dir = tmp_path / "defaults_narrow"
    defaults_dir.mkdir(parents=True, exist_ok=True)
    toml_content = """
[[param]]
name = "risk.limit"
type = "int"
default = 20
min = 0
max = 50
tighten = "none"
scope = "global"
description = "Narrowed limit"
"""
    (defaults_dir / "core.toml").write_text(toml_content.strip())
    reg2.load_defaults(defaults_dir)

    # Override should be invalidated and value reverts to default 20
    assert reg2.get("risk.limit") == 20

    # History contains status "invalidated"
    hist = reg2.history("risk.limit")
    assert any(h.status == "invalidated" for h in hist)

    # Doctor check warns about invalidated parameter
    from unittest.mock import MagicMock

    runtime = MagicMock()
    runtime.state.params_db = db_path
    check_res = _check_params_db(runtime)
    assert check_res.ok is False
    assert check_res.severity == Severity.WARNING
    assert "invalidated" in check_res.message.lower()


def test_r04_scoped_change_changes_snapshot_hash_and_spec_api(tmp_path: Path) -> None:
    """R-04: Snapshot includes name@scope overrides and changes hash; spec() returns ParamSpec."""
    db_path = tmp_path / "snapshot_params.db"
    reg = ParamRegistry(db_path)
    spec = _make_spec("strategy.risk_mult", default=1, min_val=1, max_val=10, scope="strategy")
    reg.register(spec)

    # Public spec(name) API
    retrieved_spec = reg.spec("strategy.risk_mult")
    assert retrieved_spec.name == "strategy.risk_mult"
    with pytest.raises(ParamError, match="Unknown parameter"):
        reg.spec("nonexistent.param")

    hash1, snap1 = reg.snapshot()
    assert "strategy.risk_mult" in snap1

    # Apply scoped override
    reg.set(
        "strategy.risk_mult",
        5,
        scope="strat_alpha",
        by="user",
        reason="override for strat_alpha",
    )

    hash2, snap2 = reg.snapshot()
    assert hash1 != hash2
    assert "strategy.risk_mult@strat_alpha" in snap2
    assert snap2["strategy.risk_mult@strat_alpha"] == 5
