"""Tests for algodesk.api.errors — format and hierarchy."""

from __future__ import annotations

import pytest

from algodesk.api.errors import (
    AlgoDeskError,
    ConfigError,
    JournalError,
    ManifestError,
    NotSupported,
    ParamError,
    PluginError,
    SafetyViolation,
)

_SUBCLASSES = [
    ConfigError,
    ManifestError,
    PluginError,
    NotSupported,
    ParamError,
    SafetyViolation,
    JournalError,
]


class TestAlgoDeskError:
    def test_str_format(self) -> None:
        err = AlgoDeskError(what="boom", where="here", fix="do this")
        assert str(err) == "boom | here | do this"

    def test_fields(self) -> None:
        err = AlgoDeskError(what="w", where="x", fix="y")
        assert err.what == "w"
        assert err.where == "x"
        assert err.fix == "y"

    def test_is_exception(self) -> None:
        assert issubclass(AlgoDeskError, Exception)

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(AlgoDeskError, match="boom"):
            raise AlgoDeskError("boom", "loc", "fix it")


@pytest.mark.parametrize("cls", _SUBCLASSES, ids=lambda c: c.__name__)
class TestSubclasses:
    def test_is_subclass(self, cls: type[AlgoDeskError]) -> None:
        assert issubclass(cls, AlgoDeskError)

    def test_str_format(self, cls: type[AlgoDeskError]) -> None:
        err = cls(what="w", where="x", fix="y")
        assert str(err) == "w | x | y"

    def test_catchable_as_base(self, cls: type[AlgoDeskError]) -> None:
        with pytest.raises(AlgoDeskError):
            raise cls("w", "x", "y")
