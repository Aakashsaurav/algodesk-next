"""Tests for ParamRegistry resolution, change rules, history, reset, and snapshots."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def registry(tmp_path: Path) -> Generator[Any]:
    """Fixture providing a temporary ParamRegistry in PAPER mode."""
    from algodesk.api.kinds import Mode
    from algodesk.params.registry import ParamRegistry

    db_file = tmp_path / "registry.db"
    reg = ParamRegistry(db_file, mode=Mode.PAPER)
    yield reg
    reg.close()


def test_register_idempotent_and_conflict(registry: Any) -> None:
    """Registering identical spec is idempotent; conflicting spec raises ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType

    spec1 = ParamSpec(name="test.r.limit", type=ParamType.INT, default=10, min=1, max=100)
    registry.register(spec1)
    # Identical registration: success
    registry.register(spec1)

    # Conflicting registration (different default)
    spec_conflict = ParamSpec(name="test.r.limit", type=ParamType.INT, default=20, min=1, max=100)
    with pytest.raises(ParamError) as exc_info:
        registry.register(spec_conflict)
    assert "Conflicting specification" in exc_info.value.what


def test_resolution_order(registry: Any) -> None:
    """Resolution order: scoped override -> global override -> default."""
    from algodesk.params.specs import ParamSpec, ParamType

    spec = ParamSpec(name="test.r.loss", type=ParamType.FLOAT, default=1.5, min=0.1, max=10.0)
    registry.register(spec)

    # 1. Default value
    assert registry.get("test.r.loss") == 1.5
    assert registry.get("test.r.loss", scope="strat_a") == 1.5

    # 2. Global override
    registry.set("test.r.loss", 2.0, by="trader", reason="set global override")
    assert registry.get("test.r.loss") == 2.0
    assert registry.get("test.r.loss", scope="strat_a") == 2.0

    # 3. Scoped override beats global
    registry.set(
        "test.r.loss",
        0.5,
        by="trader",
        reason="set scoped override",
        scope="strat_a",
    )
    assert registry.get("test.r.loss", scope="strat_a") == 0.5
    assert registry.get("test.r.loss") == 2.0  # global still 2.0


def test_get_unknown_param_raises(registry: Any) -> None:
    """Accessing unregistered parameter must raise ParamError."""
    from algodesk.api.errors import ParamError

    with pytest.raises(ParamError) as exc_info:
        registry.get("unknown.parameter.name")
    assert "Unknown parameter" in exc_info.value.what


def test_set_validation(registry: Any) -> None:
    """set() validates type, bounds, reason length, and unknown params."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType

    spec = ParamSpec(name="test.r.int_limit", type=ParamType.INT, default=10, min=5, max=50)
    registry.register(spec)

    # Short reason (< 5 chars)
    with pytest.raises(ParamError) as exc_info:
        registry.set("test.r.int_limit", 20, by="me", reason="abc")
    assert "Reason is mandatory" in exc_info.value.what

    # Out of bounds: below min
    with pytest.raises(ParamError) as exc_info:
        registry.set("test.r.int_limit", 4, by="me", reason="valid reason")
    assert "below minimum" in exc_info.value.what

    # Out of bounds: above max
    with pytest.raises(ParamError) as exc_info:
        registry.set("test.r.int_limit", 55, by="me", reason="valid reason")
    assert "above maximum" in exc_info.value.what

    # Wrong type
    with pytest.raises(ParamError) as exc_info:
        registry.set("test.r.int_limit", "not_int", by="me", reason="valid reason")
    assert "Expected integer" in exc_info.value.what

    # Unknown param
    with pytest.raises(ParamError) as exc_info:
        registry.set("nonexistent.param", 10, by="me", reason="valid reason")
    assert "unknown parameter" in exc_info.value.what.lower()


def test_tighten_loosen_neutral_directions(registry: Any) -> None:
    """Direction computation for down, up, and none tightening modes."""
    from algodesk.params.specs import ParamSpec, ParamType, Tighten

    # tighten: DOWN (e.g. loss limit)
    spec_down = ParamSpec(
        name="test.r.down",
        type=ParamType.FLOAT,
        default=5.0,
        min=1.0,
        max=10.0,
        tighten=Tighten.DOWN,
    )
    registry.register(spec_down)

    res1 = registry.set("test.r.down", 4.0, by="me", reason="lower is tighten")
    assert res1.direction == "tighten"

    res2 = registry.set("test.r.down", 6.0, by="me", reason="higher is loosen")
    assert res2.direction == "loosen"

    res3 = registry.set("test.r.down", 6.0, by="me", reason="same is neutral")
    assert res3.direction == "neutral"

    # tighten: UP (e.g. margin buffer)
    spec_up = ParamSpec(
        name="test.r.up",
        type=ParamType.FLOAT,
        default=20.0,
        min=5.0,
        max=50.0,
        tighten=Tighten.UP,
    )
    registry.register(spec_up)

    res4 = registry.set("test.r.up", 25.0, by="me", reason="higher is tighten")
    assert res4.direction == "tighten"

    res5 = registry.set("test.r.up", 15.0, by="me", reason="lower is loosen")
    assert res5.direction == "loosen"

    # tighten: NONE
    spec_none = ParamSpec(
        name="test.r.none",
        type=ParamType.INT,
        default=30,
        min=1,
        max=100,
        tighten=Tighten.NONE,
    )
    registry.register(spec_none)
    res6 = registry.set("test.r.none", 40, by="me", reason="any change is neutral")
    assert res6.direction == "neutral"


def test_non_live_loosening_is_immediate(registry: Any) -> None:
    """In non-live modes, loosening applies immediately without confirmation."""
    from algodesk.params.specs import ParamSpec, ParamType, Tighten

    spec = ParamSpec(
        name="test.r.limit",
        type=ParamType.FLOAT,
        default=1.5,
        min=0.1,
        max=10.0,
        tighten=Tighten.DOWN,
    )
    registry.register(spec)
    res = registry.set("test.r.limit", 3.0, by="trader", reason="relax limit for test")
    assert res.direction == "loosen"
    assert res.status == "applied"
    assert res.effective_at == "now"
    assert registry.get("test.r.limit") == 3.0


def test_reset_parameter(registry: Any) -> None:
    """reset() restores parameter to default and leaves history row."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType

    spec = ParamSpec(name="test.r.buf", type=ParamType.INT, default=10, min=1, max=100)
    registry.register(spec)
    registry.set("test.r.buf", 50, by="tester", reason="temporary override")
    assert registry.get("test.r.buf") == 50

    reset_res = registry.reset("test.r.buf", by="tester", reason="resetting to default")
    assert reset_res.status == "applied"
    assert reset_res.new_value == 10
    assert registry.get("test.r.buf") == 10

    # Short reason check for reset
    with pytest.raises(ParamError):
        registry.reset("test.r.buf", by="tester", reason="abc")


def test_history_logging_and_queries(registry: Any) -> None:
    """History tracks who, channel, reason, old/new values, and supports filtering."""
    from algodesk.params.specs import ParamSpec, ParamType

    spec = ParamSpec(name="test.r.hist", type=ParamType.INT, default=5, min=1, max=20)
    registry.register(spec)

    registry.set(
        "test.r.hist",
        10,
        by="alice",
        channel="cli",
        reason="first adjustment",
    )
    registry.set(
        "test.r.hist",
        15,
        by="bob",
        channel="telegram",
        reason="second adjustment",
    )

    hist = registry.history(name="test.r.hist", limit=10)
    assert len(hist) == 2
    # newest first
    assert hist[0].new_value == 15
    assert hist[0].old_value == 10
    assert hist[0].by == "bob"
    assert hist[0].channel == "telegram"
    assert hist[0].reason == "second adjustment"

    assert hist[1].new_value == 10
    assert hist[1].old_value == 5
    assert hist[1].by == "alice"


def test_show_rows_and_filtering(registry: Any) -> None:
    """show() returns rows with name, value, default, scope, and last change."""
    from algodesk.params.specs import ParamSpec, ParamType

    spec1 = ParamSpec(name="group.a.limit", type=ParamType.INT, default=5)
    spec2 = ParamSpec(name="group.b.limit", type=ParamType.INT, default=10)
    registry.register(spec1)
    registry.register(spec2)

    registry.set("group.a.limit", 7, by="me", reason="update group a")

    rows_all = registry.show()
    assert len(rows_all) == 2

    rows_a = registry.show(prefix="group.a")
    assert len(rows_a) == 1
    assert rows_a[0].name == "group.a.limit"
    assert rows_a[0].value == 7
    assert rows_a[0].default == 5
    assert rows_a[0].last_change is not None


def test_snapshot_hash_stable_and_changes(registry: Any) -> None:
    """snapshot() SHA-256 hash is deterministic and changes upon value updates."""
    from algodesk.params.specs import ParamSpec, ParamType

    spec = ParamSpec(name="test.r.snap", type=ParamType.INT, default=1)
    registry.register(spec)

    hash1, vals1 = registry.snapshot()
    assert len(hash1) == 64
    assert vals1["test.r.snap"] == 1

    # Same state yields identical hash
    hash2, _ = registry.snapshot()
    assert hash1 == hash2

    # Changing value changes hash
    registry.set("test.r.snap", 2, by="me", reason="modify for snapshot")
    hash3, vals3 = registry.snapshot()
    assert hash3 != hash1
    assert vals3["test.r.snap"] == 2


def test_restart_keeps_overrides_without_overwrite(tmp_path: Path) -> None:
    """Reopening registry retains stored overrides and load_defaults does not overwrite them."""
    from algodesk.api.kinds import Mode
    from algodesk.params.registry import ParamRegistry

    db_file = tmp_path / "persist.db"
    reg1 = ParamRegistry(db_file, mode=Mode.PAPER)
    reg1.load_defaults(Path("config/params"))
    reg1.set("risk.conservative.daily_loss_limit_pct", 0.75, by="admin", reason="custom override")
    reg1.close()

    # Restart with fresh registry pointing to same database
    reg2 = ParamRegistry(db_file, mode=Mode.PAPER)
    reg2.load_defaults(Path("config/params"))
    assert reg2.get("risk.conservative.daily_loss_limit_pct") == 0.75
    reg2.close()


def test_registry_context_manager(tmp_path: Path) -> None:
    """ParamRegistry works with context manager protocol."""
    from algodesk.api.kinds import Mode
    from algodesk.params.registry import ParamRegistry
    from algodesk.params.specs import ParamSpec, ParamType

    db_file = tmp_path / "cm.db"
    with ParamRegistry(db_file, mode=Mode.PAPER) as reg:
        spec = ParamSpec(name="test.r.cm", type=ParamType.INT, default=10)
        reg.register(spec)
        assert reg.get("test.r.cm") == 10


def test_history_without_name_returns_all(registry: Any) -> None:
    """history(name=None) returns audit records across all parameters."""
    from algodesk.params.specs import ParamSpec, ParamType

    spec1 = ParamSpec(name="test.r.p1", type=ParamType.INT, default=1)
    spec2 = ParamSpec(name="test.r.p2", type=ParamType.INT, default=2)
    registry.register(spec1)
    registry.register(spec2)

    registry.set("test.r.p1", 10, by="a", reason="reason 1")
    registry.set("test.r.p2", 20, by="b", reason="reason 2")

    all_hist = registry.history(name=None, limit=50)
    assert len(all_hist) == 2
    names = {h.name for h in all_hist}
    assert names == {"test.r.p1", "test.r.p2"}


def test_history_limit_respected(registry: Any) -> None:
    """history(limit=N) caps the returned entries to N."""
    from algodesk.params.specs import ParamSpec, ParamType

    spec = ParamSpec(name="test.r.lim", type=ParamType.INT, default=1)
    registry.register(spec)
    for i in range(5):
        registry.set("test.r.lim", i + 10, by="me", reason=f"update number {i}")

    limited = registry.history(name="test.r.lim", limit=2)
    assert len(limited) == 2
    assert limited[0].new_value == 14
    assert limited[1].new_value == 13


def test_reset_without_prior_override(registry: Any) -> None:
    """Resetting a parameter with no existing override leaves default intact and records history."""
    from algodesk.params.specs import ParamSpec, ParamType

    spec = ParamSpec(name="test.r.reset_def", type=ParamType.INT, default=42)
    registry.register(spec)

    res = registry.reset("test.r.reset_def", by="me", reason="reset default param")
    assert res.old_value == 42
    assert res.new_value == 42
    assert res.status == "applied"
    assert registry.get("test.r.reset_def") == 42


def test_change_result_str_and_repr(registry: Any) -> None:
    """ChangeResult has readable __str__ and __repr__ formatting."""
    from algodesk.params.specs import ParamSpec, ParamType, Tighten

    spec = ParamSpec(name="test.r.cr", type=ParamType.INT, default=10, tighten=Tighten.DOWN)
    registry.register(spec)
    res = registry.set("test.r.cr", 5, by="me", reason="tighten limit")

    repr_str = repr(res)
    assert "ChangeResult(" in repr_str
    assert "direction='tighten'" in repr_str

    str_out = str(res)
    assert "tighten, applied now" in str_out


def test_session_start_empty_pending(registry: Any) -> None:
    """session_start returns 0 when no pending changes exist."""
    assert registry.session_start() == 0
