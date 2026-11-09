"""Tests for conformance checking of broker and secrets plugins."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from algodesk.api import (
    data_source_problems,
    secrets_problems,
)
from algodesk.api.kinds import Mode
from algodesk.ops.context import build_context
from algodesk.ops.runtime import open_runtime
from algodesk.registry.base_checks import base_contract_problems


def test_conformance_secrets_keychain(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    with open_runtime(ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id="conf_keychain")
        plugin = rt.plugins.create(
            "secrets",
            "keychain",
            config={"service": "test", "index_file": str(tmp_path / "idx.json")},
            ctx=p_ctx,
        )
        assert base_contract_problems(plugin) == []
        assert secrets_problems(plugin) == []


def test_conformance_secrets_env() -> None:
    with open_runtime(ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id="conf_env")
        plugin = rt.plugins.create("secrets", "env", config={}, ctx=p_ctx)
        assert base_contract_problems(plugin) == []
        assert secrets_problems(plugin) == []


def test_conformance_broker_yfinance() -> None:
    with open_runtime(ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id="conf_yfinance")
        plugin = rt.plugins.create("broker", "yfinance", config={}, ctx=p_ctx)
        assert base_contract_problems(plugin) == []
        assert data_source_problems(plugin) == []


def test_conformance_broker_upstox() -> None:
    with open_runtime(ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id="conf_upstox")
        plugin = rt.plugins.create("broker", "upstox", config={}, ctx=p_ctx)
        assert base_contract_problems(plugin) == []
        assert data_source_problems(plugin) == []


def test_conformance_broker_importer() -> None:
    with open_runtime(ensure=False) as rt:
        p_ctx = build_context(Mode.RESEARCH, run_id="conf_importer")
        plugin = rt.plugins.create("broker", "importer", config={}, ctx=p_ctx)
        assert base_contract_problems(plugin) == []
        assert data_source_problems(plugin) == []
