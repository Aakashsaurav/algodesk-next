"""Hypothesis property tests for parameter bounds, invariants, and storage."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


@given(
    min_val=st.integers(min_value=-1000, max_value=1000),
    max_val=st.integers(min_value=-1000, max_value=1000),
    test_val=st.integers(min_value=-2000, max_value=2000),
)
@settings(max_examples=100)
def test_property_int_bounds_invariant(min_val: int, max_val: int, test_val: int) -> None:
    """No accepted int value is ever outside bounds; out-of-bounds raises ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType, validate_value

    actual_min = min(min_val, max_val)
    actual_max = max(min_val, max_val)
    default_val = actual_min

    spec = ParamSpec(
        name="prop.test.int",
        type=ParamType.INT,
        default=default_val,
        min=actual_min,
        max=actual_max,
    )

    if actual_min <= test_val <= actual_max:
        accepted = validate_value(spec, test_val)
        assert actual_min <= accepted <= actual_max
    else:
        try:
            validate_value(spec, test_val)
            raise AssertionError(
                f"Expected out-of-bounds error for {test_val} in [{actual_min}, {actual_max}]"
            )
        except ParamError:
            pass


@given(
    min_val=st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    max_val=st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False),
    test_val=st.floats(min_value=-200.0, max_value=200.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_property_float_bounds_invariant(min_val: float, max_val: float, test_val: float) -> None:
    """No accepted float value is ever outside bounds; out-of-bounds raises ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType, validate_value

    actual_min = min(min_val, max_val)
    actual_max = max(min_val, max_val)
    default_val = actual_min

    spec = ParamSpec(
        name="prop.test.float",
        type=ParamType.FLOAT,
        default=default_val,
        min=actual_min,
        max=actual_max,
    )

    if actual_min <= test_val <= actual_max:
        accepted = validate_value(spec, test_val)
        assert actual_min <= accepted <= actual_max
    else:
        try:
            validate_value(spec, test_val)
            raise AssertionError(f"Expected out-of-bounds error for {test_val}")
        except ParamError:
            pass


@given(
    bad_val=st.one_of(
        st.integers(min_value=101, max_value=500),
        st.integers(min_value=-500, max_value=-1),
        st.text(min_size=1, max_size=10),
    )
)
@settings(max_examples=50)
def test_property_no_rejected_value_stored(
    tmp_path_factory: pytest.TempPathFactory, bad_val: object
) -> None:
    """When a value is rejected, the registry store remains unmodified."""
    from algodesk.api.errors import ParamError
    from algodesk.api.kinds import Mode
    from algodesk.params.registry import ParamRegistry
    from algodesk.params.specs import ParamSpec, ParamType

    tmp_dir = tmp_path_factory.mktemp("hypo")
    db_file = tmp_dir / "reg.db"
    reg = ParamRegistry(db_file, mode=Mode.PAPER)
    spec = ParamSpec(
        name="prop.test.guarded",
        type=ParamType.INT,
        default=50,
        min=0,
        max=100,
    )
    reg.register(spec)
    initial_val = reg.get("prop.test.guarded")
    assert initial_val == 50

    try:
        reg.set("prop.test.guarded", bad_val, by="tester", reason="hypo test invalid value")
    except ParamError:
        # Crucial property: rejected value must NOT be stored
        assert reg.get("prop.test.guarded") == initial_val

    reg.close()
