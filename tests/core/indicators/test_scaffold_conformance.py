"""Tests for indicator plugin scaffolding and conformance checking with golden.toml."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from algodesk.api.conformance import indicator_problems
from algodesk.api.protocols import IndicatorSpec
from algodesk.registry.scaffold import scaffold_plugin


def test_scaffold_indicator_writes_empty_golden_toml(tmp_path: Path) -> None:
    dest = scaffold_plugin(tmp_path, "indicator", "custom_ind")
    assert (dest / "plugin.toml").exists()
    assert (dest / "plugin.py").exists()
    assert (dest / "README.md").exists()
    assert (dest / "tests" / "test_custom_ind.py").exists()
    golden_file = dest / "golden.toml"
    assert golden_file.exists()
    assert golden_file.read_text(encoding="utf-8") == ""


def test_conformance_fails_when_golden_toml_empty(tmp_path: Path) -> None:
    dest = scaffold_plugin(tmp_path, "indicator", "empty_ind")

    class DummyPlugin:
        directory = dest
        manifest = MagicMock()
        manifest.kind = "indicator"
        spec = IndicatorSpec(name="empty_ind")

        def compute(self, vals: list[float]) -> list[float]:
            return vals

        def new_state(self) -> dict[str, object]:
            return {}

        def update(self, state: object, val: float) -> float:
            return val

    probs = indicator_problems(DummyPlugin())
    assert len(probs) == 1
    assert "golden.toml has no vectors" in probs[0]
    assert "specify at least one golden vector" in probs[0]


def test_conformance_passes_when_golden_toml_populated(tmp_path: Path) -> None:
    dest = scaffold_plugin(tmp_path, "indicator", "valid_ind")
    golden_file = dest / "golden.toml"
    golden_content = """
[[vectors]]
inputs = [1.0, 2.0, 3.0]
expected = [1.0, 2.0, 3.0]
"""
    golden_file.write_text(golden_content, encoding="utf-8")

    class ValidPlugin:
        directory = dest
        manifest = MagicMock()
        manifest.kind = "indicator"
        spec = IndicatorSpec(name="valid_ind", warmup=0)

        def compute(self, vals: list[float]) -> list[float]:
            return list(vals)

        def new_state(self) -> dict[str, object]:
            return {}

        def update(self, state: object, val: float) -> float:
            return val

    probs = indicator_problems(ValidPlugin())
    assert probs == []


def test_conformance_fails_on_numerical_mismatch(tmp_path: Path) -> None:
    dest = scaffold_plugin(tmp_path, "indicator", "mismatch_ind")
    golden_file = dest / "golden.toml"
    golden_file.write_text(
        "[[vectors]]\ninputs = [1.0, 2.0, 3.0]\nexpected = [1.0, 2.0, 99.0]\n",
        encoding="utf-8",
    )

    class MismatchPlugin:
        directory = dest
        manifest = MagicMock()
        manifest.kind = "indicator"
        spec = IndicatorSpec(name="mismatch_ind", warmup=0)

        def compute(self, vals: list[float]) -> list[float]:
            return list(vals)

        def new_state(self) -> dict[str, object]:
            return {}

        def update(self, state: object, val: float) -> float:
            return val

    probs = indicator_problems(MismatchPlugin())
    assert any("numerical difference" in p.lower() or "expected" in p.lower() for p in probs)


def test_conformance_fails_on_batch_incremental_mismatch(tmp_path: Path) -> None:
    dest = scaffold_plugin(tmp_path, "indicator", "inc_mismatch")
    golden_file = dest / "golden.toml"
    golden_file.write_text(
        "[[vectors]]\ninputs = [1.0, 2.0, 3.0]\nexpected = [2.0, 4.0, 6.0]\n",
        encoding="utf-8",
    )

    class IncMismatchPlugin:
        directory = dest
        manifest = MagicMock()
        manifest.kind = "indicator"
        spec = IndicatorSpec(name="inc_mismatch", warmup=0)

        def compute(self, vals: list[float]) -> list[float]:
            return [v * 2 for v in vals]

        def new_state(self) -> dict[str, object]:
            return {}

        def update(self, state: object, val: float) -> float:
            # Buggy update returns unscaled val
            return val

    probs = indicator_problems(IncMismatchPlugin())
    assert any("numerical difference" in p.lower() or "mismatch" in p.lower() for p in probs)


def test_conformance_fails_on_warmup_violation(tmp_path: Path) -> None:
    dest = scaffold_plugin(tmp_path, "indicator", "warmup_bad")
    golden_file = dest / "golden.toml"
    golden_file.write_text(
        "[[vectors]]\ninputs = [10.0, 20.0, 30.0, 40.0]\nexpected = [10.0, 20.0, 30.0, 40.0]\n",
        encoding="utf-8",
    )

    class WarmupBadPlugin:
        directory = dest
        manifest = MagicMock()
        manifest.kind = "indicator"
        # Declares warmup=2, but outputs values at index 0 and 1
        spec = IndicatorSpec(name="warmup_bad", warmup=2)

        def compute(self, vals: list[float]) -> list[float]:
            return list(vals)

        def new_state(self) -> dict[str, object]:
            return {}

        def update(self, state: object, val: float) -> float:
            return val

    probs = indicator_problems(WarmupBadPlugin())
    assert any("warmup" in p.lower() for p in probs)


def test_conformance_fails_on_lookahead_bias(tmp_path: Path) -> None:
    dest = scaffold_plugin(tmp_path, "indicator", "lookahead_bad")
    golden_file = dest / "golden.toml"
    golden_file.write_text(
        "[[vectors]]\ninputs = [10.0, 20.0, 30.0, 40.0, 50.0]\n"
        "expected = [10.0, 20.0, 30.0, 40.0, 50.0]\n",
        encoding="utf-8",
    )

    class LookaheadBadPlugin:
        directory = dest
        manifest = MagicMock()
        manifest.kind = "indicator"
        spec = IndicatorSpec(name="lookahead_bad", warmup=0)

        def compute(self, vals: list[float]) -> list[float]:
            # Look-ahead: modifies past output based on final element!
            last_val = vals[-1]
            return [v + (1.0 if last_val > 45.0 else 0.0) for v in vals]

        def new_state(self) -> dict[str, object]:
            return {}

        def update(self, state: object, val: float) -> float:
            return val

    probs = indicator_problems(LookaheadBadPlugin())
    assert any("look-ahead" in p.lower() or "numerical difference" in p.lower() for p in probs)
