"""Tests for ParamSpec, ParamType, Tighten, validate_value, and parse_cli_value."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_valid_specs_all_types() -> None:
    """ParamSpec accepts all supported types with valid definitions."""
    from algodesk.params.specs import ParamSpec, ParamType, Tighten

    spec_int = ParamSpec(
        name="test.core.int_val",
        type=ParamType.INT,
        default=10,
        min=1,
        max=100,
        tighten=Tighten.DOWN,
    )
    assert spec_int.type == ParamType.INT
    assert spec_int.default == 10

    spec_float = ParamSpec(
        name="test.core.float_val",
        type=ParamType.FLOAT,
        default=2.5,
        min=0.0,
        max=10.0,
        tighten=Tighten.UP,
    )
    assert spec_float.default == 2.5

    spec_bool = ParamSpec(
        name="test.core.bool_val",
        type=ParamType.BOOL,
        default=True,
    )
    assert spec_bool.default is True

    spec_str = ParamSpec(
        name="test.core.str_val",
        type=ParamType.STR,
        default="active",
    )
    assert spec_str.default == "active"

    spec_enum = ParamSpec(
        name="test.core.enum_val",
        type=ParamType.ENUM,
        default="conservative",
        choices=["conservative", "moderate", "aggressive"],
    )
    assert spec_enum.default == "conservative"

    spec_time = ParamSpec(
        name="test.core.time_val",
        type=ParamType.TIME,
        default="09:15",
    )
    assert spec_time.default == "09:15"


def test_invalid_names() -> None:
    """Invalid parameter names without dot namespace or with uppercase must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType

    invalid_names = [
        "nodot",
        "UPPER.case",
        ".leading.dot",
        "trailing.dot.",
        "two..dots",
        "has-hyphen.val",
        "has space.val",
    ]
    for bad_name in invalid_names:
        with pytest.raises(ParamError) as exc_info:
            ParamSpec(name=bad_name, type=ParamType.INT, default=1)
        assert "Invalid parameter name" in exc_info.value.what


def test_unknown_type_and_tighten() -> None:
    """Unknown type or tighten strings in ParamSpec must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec

    with pytest.raises(ParamError) as exc_info:
        ParamSpec.model_validate({"name": "test.core.param", "type": "complex", "default": 1})
    assert "Unknown parameter type" in exc_info.value.what

    with pytest.raises(ParamError) as exc_info:
        ParamSpec.model_validate(
            {
                "name": "test.core.param",
                "type": "int",
                "default": 1,
                "tighten": "sideways",
            }
        )
    assert "Unknown tighten direction" in exc_info.value.what


def test_enum_without_choices_or_empty_choices() -> None:
    """Enum type must provide non-empty choices list."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType

    with pytest.raises(ParamError) as exc_info:
        ParamSpec(name="test.core.enum_spec", type=ParamType.ENUM, default="a")
    assert "Enum parameter requires non-empty choices" in exc_info.value.what

    with pytest.raises(ParamError) as exc_info:
        ParamSpec(
            name="test.core.enum_spec",
            type=ParamType.ENUM,
            default="a",
            choices=[],
        )
    assert "Enum parameter requires non-empty choices" in exc_info.value.what


def test_choices_on_non_enum_rejected() -> None:
    """Non-enum types cannot specify choices."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType

    with pytest.raises(ParamError) as exc_info:
        ParamSpec(
            name="test.core.int_spec",
            type=ParamType.INT,
            default=5,
            choices=["5", "10"],
        )
    assert "Choices are only supported for enum parameters" in exc_info.value.what


def test_min_greater_than_max_rejected() -> None:
    """min > max must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType

    with pytest.raises(ParamError) as exc_info:
        ParamSpec(
            name="test.core.bounds",
            type=ParamType.INT,
            default=5,
            min=10,
            max=5,
        )
    assert "exceeds maximum bound" in exc_info.value.what


def test_default_type_mismatch() -> None:
    """Default value of incorrect type must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType

    # bool is never accepted as int
    with pytest.raises(ParamError) as exc_info:
        ParamSpec(name="test.core.val", type=ParamType.INT, default=True)
    assert "bool is not accepted as int" in exc_info.value.fix

    # float not accepted for int
    with pytest.raises(ParamError) as exc_info:
        ParamSpec(name="test.core.val", type=ParamType.INT, default=2.5)
    assert "Expected integer" in exc_info.value.what

    # string not accepted for int
    with pytest.raises(ParamError) as exc_info:
        ParamSpec(name="test.core.val", type=ParamType.INT, default="10")
    assert "Expected integer" in exc_info.value.what

    # int accepted as float
    spec = ParamSpec(name="test.core.val", type=ParamType.FLOAT, default=10)
    assert isinstance(spec.default, float)
    assert spec.default == 10.0


def test_default_out_of_bounds() -> None:
    """Default value outside [min, max] must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType

    with pytest.raises(ParamError) as exc_info:
        ParamSpec(name="test.core.val", type=ParamType.INT, default=0, min=1, max=10)
    assert "below minimum" in exc_info.value.what

    with pytest.raises(ParamError) as exc_info:
        ParamSpec(name="test.core.val", type=ParamType.INT, default=15, min=1, max=10)
    assert "above maximum" in exc_info.value.what


def test_default_not_in_choices() -> None:
    """Default enum value not in choices must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType

    with pytest.raises(ParamError) as exc_info:
        ParamSpec(
            name="test.core.val",
            type=ParamType.ENUM,
            default="unknown",
            choices=["a", "b"],
        )
    assert "is not in choices" in exc_info.value.what


def test_default_invalid_time_format() -> None:
    """Default time string with invalid HH:MM format must raise ParamError."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType

    bad_times = ["25:00", "12:60", "9:15", "noon", "12:00:00"]
    for bad in bad_times:
        with pytest.raises(ParamError) as exc_info:
            ParamSpec(name="test.core.schedule", type=ParamType.TIME, default=bad)
        assert "Invalid time format" in exc_info.value.what


def test_validate_value_bounds() -> None:
    """validate_value enforces boundaries accurately."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType, validate_value

    spec = ParamSpec(
        name="test.core.limit",
        type=ParamType.FLOAT,
        default=5.0,
        min=1.0,
        max=10.0,
    )
    assert validate_value(spec, 1.0) == 1.0
    assert validate_value(spec, 10.0) == 10.0
    assert validate_value(spec, 5) == 5.0

    with pytest.raises(ParamError) as exc_info:
        validate_value(spec, 0.9)
    assert "below minimum" in exc_info.value.what

    with pytest.raises(ParamError) as exc_info:
        validate_value(spec, 10.1)
    assert "above maximum" in exc_info.value.what


def test_parse_cli_value_bool() -> None:
    """parse_cli_value accurately parses various boolean representations."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType, parse_cli_value

    spec = ParamSpec(name="test.core.flag", type=ParamType.BOOL, default=False)
    for raw in ["true", "True", "t", "yes", "YES", "1", "y"]:
        assert parse_cli_value(spec, raw) is True
    for raw in ["false", "False", "f", "no", "NO", "0", "n"]:
        assert parse_cli_value(spec, raw) is False

    with pytest.raises(ParamError) as exc_info:
        parse_cli_value(spec, "maybe")
    assert "Cannot parse 'maybe' as boolean" in exc_info.value.what


def test_parse_cli_value_numeric() -> None:
    """parse_cli_value converts numeric strings and validates bounds."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType, parse_cli_value

    spec_int = ParamSpec(
        name="test.core.count",
        type=ParamType.INT,
        default=5,
        min=1,
        max=20,
    )
    assert parse_cli_value(spec_int, "15") == 15
    with pytest.raises(ParamError) as exc_info:
        parse_cli_value(spec_int, "abc")
    assert "Cannot parse 'abc' as integer" in exc_info.value.what

    spec_float = ParamSpec(
        name="test.core.pct",
        type=ParamType.FLOAT,
        default=2.0,
        min=0.0,
        max=100.0,
    )
    assert parse_cli_value(spec_float, "3.14") == 3.14
    with pytest.raises(ParamError) as exc_info:
        parse_cli_value(spec_float, "xyz")
    assert "Cannot parse 'xyz' as float" in exc_info.value.what


def test_bool_rejected_as_float() -> None:
    """Boolean values must never be accepted as float."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType, validate_value

    spec = ParamSpec(name="test.core.float_val", type=ParamType.FLOAT, default=1.0)
    with pytest.raises(ParamError) as exc_info:
        validate_value(spec, True)
    assert "Expected float or int" in exc_info.value.what


def test_validate_value_enum_and_time() -> None:
    """validate_value validates enum choices and time format."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType, validate_value

    enum_spec = ParamSpec(
        name="test.core.mode",
        type=ParamType.ENUM,
        default="a",
        choices=["a", "b"],
    )
    assert validate_value(enum_spec, "b") == "b"
    with pytest.raises(ParamError) as exc_info:
        validate_value(enum_spec, "c")
    assert "is not in choices" in exc_info.value.what

    time_spec = ParamSpec(name="test.core.start", type=ParamType.TIME, default="09:15")
    assert validate_value(time_spec, "15:30") == "15:30"
    with pytest.raises(ParamError) as exc_info:
        validate_value(time_spec, "24:00")
    assert "Invalid time format" in exc_info.value.what


def test_parse_cli_value_enum_and_time() -> None:
    """parse_cli_value parses strings into valid enum choices and time formats."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType, parse_cli_value

    enum_spec = ParamSpec(
        name="test.core.mode",
        type=ParamType.ENUM,
        default="paper",
        choices=["paper", "live"],
    )
    assert parse_cli_value(enum_spec, "live") == "live"
    with pytest.raises(ParamError) as exc_info:
        parse_cli_value(enum_spec, "backtest")
    assert "is not in choices" in exc_info.value.what

    time_spec = ParamSpec(name="test.core.time", type=ParamType.TIME, default="09:15")
    assert parse_cli_value(time_spec, "09:30") == "09:30"


def test_param_spec_is_frozen() -> None:
    """ParamSpec instance is immutable."""
    from algodesk.params.specs import ParamSpec, ParamType

    spec = ParamSpec(name="test.core.frozen", type=ParamType.INT, default=1)
    with pytest.raises(ValidationError):
        spec.default = 2  # type: ignore[misc]


def test_param_spec_equality() -> None:
    """ParamSpec equality checks all fields."""
    from algodesk.params.specs import ParamSpec, ParamType

    s1 = ParamSpec(name="test.core.val", type=ParamType.INT, default=5, min=1, max=10)
    s2 = ParamSpec(name="test.core.val", type=ParamType.INT, default=5, min=1, max=10)
    s3 = ParamSpec(name="test.core.val", type=ParamType.INT, default=6, min=1, max=10)
    assert s1 == s2
    assert s1 != s3


def test_validate_value_str_type_mismatch() -> None:
    """STR type rejects integers and booleans."""
    from algodesk.api.errors import ParamError
    from algodesk.params.specs import ParamSpec, ParamType, validate_value

    spec = ParamSpec(name="test.core.label", type=ParamType.STR, default="hello")
    with pytest.raises(ParamError) as exc_info:
        validate_value(spec, 123)
    assert "Expected string" in exc_info.value.what

    with pytest.raises(ParamError) as exc_info:
        validate_value(spec, True)
    assert "Expected string" in exc_info.value.what


def test_time_valid_edge_cases() -> None:
    """TIME type validates midnight, end of day, and minute ranges."""
    from algodesk.params.specs import ParamSpec, ParamType, validate_value

    spec = ParamSpec(name="test.core.time", type=ParamType.TIME, default="00:00")
    assert validate_value(spec, "00:00") == "00:00"
    assert validate_value(spec, "23:59") == "23:59"
    assert validate_value(spec, "12:00") == "12:00"


def test_parse_cli_value_whitespace_tolerance() -> None:
    """parse_cli_value strips leading and trailing whitespaces."""
    from algodesk.params.specs import ParamSpec, ParamType, parse_cli_value

    spec_int = ParamSpec(name="test.core.int", type=ParamType.INT, default=1)
    assert parse_cli_value(spec_int, "   42   ") == 42

    spec_float = ParamSpec(name="test.core.flt", type=ParamType.FLOAT, default=1.0)
    assert parse_cli_value(spec_float, "  3.1415  ") == 3.1415

    spec_bool = ParamSpec(name="test.core.b", type=ParamType.BOOL, default=False)
    assert parse_cli_value(spec_bool, "  true  ") is True


def test_param_error_attributes_and_str() -> None:
    """ParamError preserves what, where, and fix attributes in its str representation."""
    from algodesk.api.errors import ParamError

    err = ParamError(what="Something failed", where="module.spec", fix="Do this")
    assert err.what == "Something failed"
    assert err.where == "module.spec"
    assert err.fix == "Do this"
    assert str(err) == "Something failed | module.spec | Do this"
