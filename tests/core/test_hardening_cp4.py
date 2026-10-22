"""Hardening tests for CP4: R-06, R-07, R-10, R-11, R-12, R-15, R-17."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from algodesk.api.checks import CheckResult, DoctorCheck, Severity
from algodesk.api.errors import ConfigError, NotSupported, PluginError
from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.scrub import Scrubber
from algodesk.journal.store import runs
from algodesk.journal.writer import Journal, JournalSettings
from algodesk.logs.setup import JsonLogFormatter, setup_logging
from algodesk.ops.doctor import DoctorRegistry
from algodesk.ops.paths import StatePaths
from algodesk.registry.registry import PluginRegistry
from algodesk.registry.scaffold import scaffold_plugin

# ---------------------------------------------------------------------------
# R-06: Scrubber hex preservation vs masking
# ---------------------------------------------------------------------------


def test_scrubber_preserves_40_and_64_hex_unless_secret_key() -> None:
    s = Scrubber()
    git_sha = "da39a3ee5e6b4b0d3255bfef95601890afd80709"  # 40 hex chars
    sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # 64 hex chars
    md5_32 = "d41d8cd98f00b204e9800998ecf8427e"  # 32 hex chars

    # Standalone or non-secret contexts: 40 and 64 hex survive
    assert s.scrub(f"commit {git_sha} deployed") == f"commit {git_sha} deployed"
    assert s.scrub(f"checksum {sha256} verified") == f"checksum {sha256} verified"
    # 32 hex should still be masked
    assert s.scrub(f"hash {md5_32}") == "hash ***"

    # In dictionaries
    obj = {"config_hash": sha256, "git_commit": git_sha, "md5": md5_32}
    scrubbed = s.scrub_obj(obj)
    assert scrubbed["config_hash"] == sha256
    assert scrubbed["git_commit"] == git_sha
    assert scrubbed["md5"] == "***"

    # Under secret-like keys, they MUST be masked
    assert s.scrub(f"api_key = {sha256}") == "api_key = ***"
    assert s.scrub(f"token: {git_sha}") == "token: ***"

    secret_obj = {"api_key": sha256, "auth_token": git_sha}
    secret_scrubbed = s.scrub_obj(secret_obj)
    assert secret_scrubbed["api_key"] == "***"
    assert secret_scrubbed["auth_token"] == "***"


def test_sha256_survives_journal_payload_and_log_line(tmp_path: Path) -> None:
    s = Scrubber()
    sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    # Journal payload
    db_path = tmp_path / "journal.db"
    writer = Journal(db_path, settings=JournalSettings(50, 1000, 5000), scrubber=s)
    writer.start_run("paper", "0.1.0", "hash1", "hash2")
    writer.append("order_placed", {"order_id": "ORD123", "code_hash": sha256})
    writer.flush()
    writer.close()

    from algodesk.journal.store import events

    evs = events(db_path)
    order_ev = next(e for e in evs if e["kind"] == "order_placed")
    payload = json.loads(order_ev["payload_json"])
    assert payload["code_hash"] == sha256

    # Log line via JsonLogFormatter
    formatter = JsonLogFormatter("run-sha-test")
    record = logging.LogRecord(
        name="algodesk.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="hash check",
        args=(),
        exc_info=None,
    )
    record.__dict__["code_hash"] = sha256
    formatted = formatter.format(record)
    data = json.loads(formatted)
    assert data["code_hash"] == sha256


# ---------------------------------------------------------------------------
# R-07: Log formatter default=str and invalid level ConfigError
# ---------------------------------------------------------------------------


class NonSerializableCustom:
    def __str__(self) -> str:
        return "custom-object-repr"


def test_json_log_formatter_uses_default_str() -> None:
    formatter = JsonLogFormatter("run-1")
    record = logging.LogRecord(
        name="algodesk.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="test message",
        args=(),
        exc_info=None,
    )
    record.__dict__["custom_obj"] = NonSerializableCustom()
    # Must not raise TypeError: Object of type NonSerializableCustom is not JSON serializable
    formatted = formatter.format(record)
    data = json.loads(formatted)
    assert data["custom_obj"] == "custom-object-repr"


def test_setup_logging_invalid_level_raises_config_error(tmp_path: Path) -> None:
    s = Scrubber()
    with pytest.raises(ConfigError) as exc_info:
        setup_logging(
            level="NOT_A_LEVEL",
            fmt="json",
            log_dir=tmp_path,
            run_id="run-test",
            scrubber=s,
            retention_days=7,
        )
    assert "Invalid log level" in exc_info.value.what


# ---------------------------------------------------------------------------
# R-12: CheckResult optional severity used by DoctorRegistry
# ---------------------------------------------------------------------------


def test_check_result_severity_overrides_check_severity() -> None:
    def check_fn() -> CheckResult:
        # Check has default severity CRITICAL, but result specifies WARNING
        return CheckResult(
            ok=False,
            message="soft warning",
            fix="ignore or adjust",
            severity=Severity.WARNING,
        )

    doc_check = DoctorCheck(
        id="test.soft_fail",
        severity=Severity.CRITICAL,
        run=check_fn,
    )

    registry = DoctorRegistry()
    registry.register(doc_check)
    report = registry.run()

    assert report.warning == 1
    assert report.critical == 0
    assert report.results[0].severity == Severity.WARNING


# ---------------------------------------------------------------------------
# R-11: StatePaths keyword-only constructor and resolve()
# ---------------------------------------------------------------------------


def test_state_paths_keyword_only_and_resolve(tmp_path: Path) -> None:
    # Keyword-only initialization works
    sp = StatePaths(
        params_db=tmp_path / "params.db",
        journal_db=tmp_path / "journal.db",
        conformance_dir=tmp_path / "conformance",
    )
    assert sp.params_db == tmp_path / "params.db"
    assert sp.journal_db == tmp_path / "journal.db"
    assert sp.conformance_dir == tmp_path / "conformance"

    # Positional invocation must raise TypeError
    with pytest.raises(TypeError):
        StatePaths(tmp_path / "p.db", tmp_path / "j.db", tmp_path / "conf")  # type: ignore[misc]

    # resolve() works
    sp_res = StatePaths.resolve(tmp_path, tmp_path / "data")
    assert sp_res.params_db == (tmp_path / "data" / "state" / "params.db").resolve()
    assert sp_res.journal_db == (tmp_path / "data" / "state" / "journal.db").resolve()
    assert sp_res.conformance_dir == (tmp_path / ".algodesk").resolve()


# ---------------------------------------------------------------------------
# R-10: Conformance status, public runs(), CLI public APIs
# ---------------------------------------------------------------------------


def test_plugin_registry_conformance_status(tmp_path: Path) -> None:
    reg = PluginRegistry(tmp_path, [tmp_path / "plugins"], state_dir=tmp_path / "state")
    # Non-existent plugin should raise PluginError on info/conformance_status
    with pytest.raises(PluginError):
        reg.conformance_status(PluginKind.BROKER, "nonexistent")

    # Scaffold a plugin and check conformance status
    plugin_dir = scaffold_plugin(tmp_path / "plugins", PluginKind.VENUE, "demo_venue")
    reg.refresh()

    # Initial status is 'none'
    assert reg.conformance_status(PluginKind.VENUE, "demo_venue") == "none"

    # Record pass -> 'current'
    reg.record_conformance(PluginKind.VENUE, "demo_venue", suite="unit")
    assert reg.conformance_status(PluginKind.VENUE, "demo_venue") == "current"

    # Modify code -> 'stale'
    (plugin_dir / "plugin.py").write_text("# modified\n", encoding="utf-8")
    reg.refresh()
    assert reg.conformance_status(PluginKind.VENUE, "demo_venue") == "stale"


def test_journal_store_runs(tmp_path: Path) -> None:
    db_path = tmp_path / "journal.db"
    writer = Journal(db_path, settings=JournalSettings(50, 1000, 5000))
    run_1 = writer.start_run("paper", "0.1.0", "h1", "h2")
    writer.end_run("ok")
    run_2 = writer.start_run("backtest", "0.1.0", "h1", "h2")
    writer.close()

    recorded = runs(db_path)
    assert len(recorded) == 2
    assert recorded[0]["run_id"] == run_1
    assert recorded[1]["run_id"] == run_2


# ---------------------------------------------------------------------------
# R-15: Scaffold validation
# ---------------------------------------------------------------------------


def test_scaffold_plugin_validates_kind_and_name(tmp_path: Path) -> None:
    # Invalid kind raises PluginError
    with pytest.raises(PluginError) as exc_kind:
        scaffold_plugin(tmp_path, "invalid_kind", "good_name")
    assert "Invalid plugin kind" in exc_kind.value.what

    # Invalid names raise PluginError
    for bad_name in ["", "BadName", "123num", "a", "name-with-dash", "a" * 50]:
        with pytest.raises(PluginError) as exc_name:
            scaffold_plugin(tmp_path, PluginKind.STRATEGY, bad_name)
        assert "Invalid plugin name" in exc_name.value.what

    # Valid kind and name succeed
    target = scaffold_plugin(tmp_path, "strategy", "valid_strat_1")
    assert target.is_dir()
    assert (target / "plugin.toml").is_file()


# ---------------------------------------------------------------------------
# R-17: PluginManifest.require() message format
# ---------------------------------------------------------------------------


def test_manifest_require_message_syntax() -> None:
    manifest = PluginManifest(
        name="test_strat",
        kind=PluginKind.STRATEGY,
        version="1.0.0",
        api_version=1,
        entry="pkg.mod:Class",
        capabilities={"streaming": False},
    )
    with pytest.raises(NotSupported) as exc_info:
        manifest.require("streaming")
    assert exc_info.value.fix == "Set streaming = true under [capabilities] in plugin.toml"
