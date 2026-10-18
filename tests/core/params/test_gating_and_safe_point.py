"""Tests for armed live gating, session_start, subscribers, and safe_point cross-process reload."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def test_armed_live_loosening_requires_confirm(tmp_path: Path) -> None:
    """Loosening while armed in LIVE mode requires typed confirm phrase."""
    from algodesk.api.errors import ParamError
    from algodesk.api.kinds import Mode
    from algodesk.params.registry import ParamRegistry
    from algodesk.params.specs import ParamSpec, ParamType, Tighten

    db_file = tmp_path / "live.db"
    reg = ParamRegistry(db_file, mode=Mode.LIVE, armed=lambda: True)
    spec = ParamSpec(
        name="risk.limit.daily",
        type=ParamType.FLOAT,
        default=1.5,
        min=0.1,
        max=10.0,
        tighten=Tighten.DOWN,
    )
    reg.register(spec)

    # 1. Loosening without confirm raises ParamError
    with pytest.raises(ParamError) as exc_info:
        reg.set("risk.limit.daily", 2.0, by="trader", reason="relaxing limit")
    assert "requires typed confirmation" in exc_info.value.what

    # 2. Loosening with wrong confirm phrase raises ParamError
    with pytest.raises(ParamError) as exc_info:
        reg.set(
            "risk.limit.daily",
            2.0,
            by="trader",
            reason="relaxing limit",
            confirm="LOOSEN",
        )
    assert "requires typed confirmation" in exc_info.value.what

    # 3. Loosening with correct confirm phrase stages as pending
    res = reg.set(
        "risk.limit.daily",
        2.0,
        by="trader",
        reason="relaxing limit",
        confirm="LOOSEN risk.limit.daily",
    )
    assert res.status == "pending"
    assert res.effective_at == "session_start"
    assert res.direction == "loosen"

    # Value is invisible to get() until session_start
    assert reg.get("risk.limit.daily") == 1.5

    # Pending history row exists
    hist = reg.history(name="risk.limit.daily")
    assert len(hist) == 1
    assert hist[0].status == "pending"
    assert hist[0].confirmed == "LOOSEN risk.limit.daily"

    # 4. Applying at session_start()
    applied_count = reg.session_start()
    assert applied_count == 1
    assert reg.get("risk.limit.daily") == 2.0

    # History row for applied now exists
    hist_after = reg.history(name="risk.limit.daily")
    assert len(hist_after) == 2
    assert hist_after[0].status == "applied"

    reg.close()


def test_armed_live_tightening_is_immediate(tmp_path: Path) -> None:
    """Tightening while armed in LIVE mode takes effect immediately without confirm."""
    from algodesk.api.kinds import Mode
    from algodesk.params.registry import ParamRegistry
    from algodesk.params.specs import ParamSpec, ParamType, Tighten

    db_file = tmp_path / "live_tighten.db"
    reg = ParamRegistry(db_file, mode=Mode.LIVE, armed=lambda: True)
    spec = ParamSpec(
        name="risk.limit.daily",
        type=ParamType.FLOAT,
        default=2.0,
        min=0.1,
        max=10.0,
        tighten=Tighten.DOWN,
    )
    reg.register(spec)

    # Tightening (2.0 -> 1.0) applies at once
    res = reg.set("risk.limit.daily", 1.0, by="trader", reason="tightening risk limit")
    assert res.direction == "tighten"
    assert res.status == "applied"
    assert res.effective_at == "now"
    assert reg.get("risk.limit.daily") == 1.0
    reg.close()


def test_unarmed_live_loosening_is_immediate(tmp_path: Path) -> None:
    """When LIVE but NOT armed, loosening applies immediately without confirm."""
    from algodesk.api.kinds import Mode
    from algodesk.params.registry import ParamRegistry
    from algodesk.params.specs import ParamSpec, ParamType, Tighten

    db_file = tmp_path / "live_unarmed.db"
    reg = ParamRegistry(db_file, mode=Mode.LIVE, armed=lambda: False)
    spec = ParamSpec(
        name="risk.limit.daily",
        type=ParamType.FLOAT,
        default=1.5,
        min=0.1,
        max=10.0,
        tighten=Tighten.DOWN,
    )
    reg.register(spec)

    res = reg.set("risk.limit.daily", 3.0, by="trader", reason="relaxing unarmed limit")
    assert res.status == "applied"
    assert res.effective_at == "now"
    assert reg.get("risk.limit.daily") == 3.0
    reg.close()


def test_subscribers_fire_once_and_survive_failing_callback(tmp_path: Path) -> None:
    """Subscribers fire once per safe_point; failing callback does not break others."""
    from algodesk.api.kinds import Mode
    from algodesk.params.registry import ParamRegistry
    from algodesk.params.specs import ParamSpec, ParamType

    db_file = tmp_path / "sub.db"
    reg = ParamRegistry(db_file, mode=Mode.PAPER)
    spec = ParamSpec(name="sub.test.val", type=ParamType.INT, default=10)
    reg.register(spec)

    events: list[tuple[str, Any]] = []

    def good_subscriber(name: str, val: Any) -> None:
        events.append((name, val))

    def bad_subscriber(name: str, val: Any) -> None:
        raise RuntimeError("boom")

    unsub_good = reg.subscribe(good_subscriber)
    reg.subscribe(bad_subscriber)

    # Set value
    reg.set("sub.test.val", 20, by="me", reason="update 1")
    assert len(events) == 0  # not fired until safe_point

    # Trigger safe_point
    changed = reg.safe_point()
    assert changed == 1
    assert events == [("sub.test.val", 20)]

    # Calling safe_point again without changes does not re-fire
    assert reg.safe_point() == 0
    assert len(events) == 1

    # Unsubscribe stops notifications
    unsub_good()
    reg.set("sub.test.val", 30, by="me", reason="update 2")
    reg.safe_point()
    assert len(events) == 1

    reg.close()


def test_safe_point_cross_process_reload(tmp_path: Path) -> None:
    """Two registries on same database communicate parameter changes at safe_point."""
    from algodesk.api.kinds import Mode
    from algodesk.params.registry import ParamRegistry
    from algodesk.params.specs import ParamSpec, ParamType

    db_file = tmp_path / "shared.db"
    r1 = ParamRegistry(db_file, mode=Mode.PAPER)
    r2 = ParamRegistry(db_file, mode=Mode.PAPER)

    spec = ParamSpec(name="shared.limit", type=ParamType.INT, default=100)
    r1.register(spec)

    # R2 gets the spec from DB at safe_point
    assert r1.get("shared.limit") == 100

    r2_events: list[tuple[str, Any]] = []
    r2.subscribe(lambda n, v: r2_events.append((n, v)))

    # R1 modifies parameter
    r1.set("shared.limit", 150, by="proc1", reason="cross process update")
    assert r1.get("shared.limit") == 150

    # Before R2 calls safe_point, R2 hasn't fired subscribers
    assert len(r2_events) == 0

    # R2 calls safe_point -> reloads and fires subscriber
    changed_count = r2.safe_point()
    assert changed_count == 1
    assert r2_events == [("shared.limit", 150)]
    assert r2.get("shared.limit") == 150

    r1.close()
    r2.close()
