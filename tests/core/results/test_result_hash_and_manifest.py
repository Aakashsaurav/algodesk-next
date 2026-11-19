"""Tests for Step 8: Deterministic result hash and manifest.json covering all 8 components."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from algodesk.results.writer import ResultsWriter


def _create_standard_manifest(
    tmp_path: Path,
    run_id: str,
    *,
    seed: int | None = 42,
    checksums: list[str] | None = None,
    universe_hash: str = "uni_hash_1",
    strategy_param_fast: int = 10,
) -> tuple[Path, dict[str, Any]]:
    writer = ResultsWriter(run_id=run_id, results_dir=tmp_path / "results")
    run_dir = tmp_path / "results" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "readiness.json").write_text(
        json.dumps({"universe": {"hash": universe_hash}}), encoding="utf-8"
    )

    manifest_info = {
        "mode": "backtest",
        "code_version": "0.1.0",
        "seed": seed,
        "checksums": checksums or ["bar_checksum_aaa", "bar_checksum_bbb"],
        "action_hash": "corp_action_123",
        "plugin_versions": {
            "fees": {"hash": "fee_sched_hash_v1"},
            "strategy": {"hash": "strat_code_hash_v1"},
        },
    }

    effective_config = (
        f"strategy = 'sma_cross'\nfast_period = {strategy_param_fast}\nslow_period = 30\n"
    )

    out_path = writer.write(
        summary_info={"mode": "backtest", "run_name": "test_hash"},
        metrics={"trades": 40},
        trades=[],
        equity_points=[],
        effective_config_text=effective_config,
        registry_snapshot={"hash": "reg_hash_999", "params": {"backtest.slippage_bps": 5}},
        manifest_info=manifest_info,
    )

    man = json.loads((out_path / "manifest.json").read_text(encoding="utf-8"))
    return out_path, man


def test_manifest_lists_all_eight_components(tmp_path: Path) -> None:
    """manifest.json explicitly lists all 8 required inputs."""
    _, man = _create_standard_manifest(tmp_path, "run_all_components")

    assert "universe_hash" in man
    assert "dataset_checksums" in man
    assert "action_hash" in man
    assert "fee_schedule_hash" in man
    assert "strategy_file_hash" in man
    assert "strategy_parameters" in man
    assert "registry_snapshot_hash" in man
    assert "model_settings" in man
    assert "seed" in man
    assert "code_version" in man
    assert "result_hash" in man


def test_two_identical_runs_produce_equal_result_hash(tmp_path: Path) -> None:
    """Two backtest runs with identical inputs produce bit-identical result_hash."""
    _, man_a = _create_standard_manifest(tmp_path, "run_identical_a")
    _, man_b = _create_standard_manifest(tmp_path, "run_identical_b")

    assert man_a["result_hash"] == man_b["result_hash"]


def test_change_one_stored_bar_changes_result_hash(tmp_path: Path) -> None:
    """Changing a dataset checksum (e.g. from an edited historical bar) alters result_hash."""
    _, man_a = _create_standard_manifest(
        tmp_path, "run_bar_a", checksums=["bar_checksum_aaa", "bar_checksum_bbb"]
    )
    _, man_b = _create_standard_manifest(
        tmp_path, "run_bar_b", checksums=["bar_checksum_aaa", "bar_checksum_MODIFIED"]
    )

    assert man_a["result_hash"] != man_b["result_hash"]


def test_change_strategy_parameter_changes_result_hash(tmp_path: Path) -> None:
    """Changing strategy parameter changes strategy_parameters and result_hash."""
    _, man_a = _create_standard_manifest(tmp_path, "run_param_a", strategy_param_fast=10)
    _, man_b = _create_standard_manifest(tmp_path, "run_param_b", strategy_param_fast=20)

    assert man_a["result_hash"] != man_b["result_hash"]


def test_change_only_seed_only_seeded_parts_differ(tmp_path: Path) -> None:
    """Changing only the run seed leaves data, fee, and param inputs identical
    while altering result_hash.
    """
    _, man_a = _create_standard_manifest(tmp_path, "run_seed_a", seed=101)
    _, man_b = _create_standard_manifest(tmp_path, "run_seed_b", seed=202)

    assert man_a["universe_hash"] == man_b["universe_hash"]
    assert man_a["dataset_checksums"] == man_b["dataset_checksums"]
    assert man_a["fee_schedule_hash"] == man_b["fee_schedule_hash"]
    assert man_a["strategy_file_hash"] == man_b["strategy_file_hash"]
    assert man_a["strategy_parameters"] == man_b["strategy_parameters"]
    assert man_a["registry_snapshot_hash"] == man_b["registry_snapshot_hash"]
    assert man_a["model_settings"] == man_b["model_settings"]
    assert man_a["code_version"] == man_b["code_version"]

    assert man_a["seed"] != man_b["seed"]
    assert man_a["result_hash"] != man_b["result_hash"]
