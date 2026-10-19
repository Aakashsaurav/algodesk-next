"""M0 Definition of Done end-to-end acceptance test suite (design §5.3, CP3)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from algodesk.api.errors import ConfigError, ParamError, PluginError, SafetyViolation
from algodesk.api.kinds import Mode
from algodesk.api.services import PluginContext
from algodesk.cli.app import app
from algodesk.devtools.archlint import run_archlint
from algodesk.devtools.budget import run_budget
from algodesk.devtools.docs import run_docs_check
from algodesk.devtools.secretscan import run_secretscan
from algodesk.journal.store import verify_journal
from algodesk.journal.writer import Journal, JournalSettings
from algodesk.ops.runtime import open_runtime
from algodesk.params.registry import ParamRegistry
from algodesk.params.specs import ParamSpec, ParamType, Tighten
from algodesk.registry.registry import PluginRegistry

# --- Helper fixtures ---


class DummyParamReader:
    def get(self, name: str, scope: str | None = None) -> Any:
        return 100


class DummyJournalWriter:
    def append(self, kind: str, payload: Any, *, durable: bool = False) -> int:
        return 1


class DummySecretReader:
    def get(self, name: str) -> str:
        return "secret_val"


def _build_context(mode: Mode) -> PluginContext:
    return PluginContext(
        mode=mode,
        run_id="acc-run-001",
        params=DummyParamReader(),
        journal=DummyJournalWriter(),
        secrets=DummySecretReader(),
    )


def _scaffold_project(tmp_path: Path, name: str = "acc_proj") -> Path:
    proj = tmp_path / name
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "algodesk"\nversion = "0.0.0"\n'
        'requires-python = ">=3.13,<3.14"\ndependencies = []\n',
        encoding="utf-8",
    )
    (proj / "README.md").write_text(
        "## Summary\n\nValid temporary workspace summary.\n", encoding="utf-8"
    )
    (proj / "AGENTS.md").write_text(
        "## Summary\n\nValid agents documentation summary.\n", encoding="utf-8"
    )

    cfg = proj / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "platform.toml").write_text(
        'project = "algodesk"\n[paths]\ndata_dir = "data"\n'
        'logs_dir = "logs"\nresults_dir = "results"\nlocal_dir = ".local"\n',
        encoding="utf-8",
    )
    params_dir = cfg / "params"
    params_dir.mkdir(parents=True, exist_ok=True)
    (params_dir / "core.toml").write_text(
        '[[param]]\nname = "ops.min_free_disk_gb"\ntype = "int"\ndefault = 5\n\n'
        '[[param]]\nname = "risk.limit.daily"\ntype = "float"\ndefault = 2.0\ntighten = "down"\n'
        '[[param]]\nname = "budget.core_lines_guidance"\ntype = "int"\ndefault = 20000\n'
        '[[param]]\nname = "budget.core_modules_guidance"\ntype = "int"\ndefault = 100\n'
        '[[param]]\nname = "budget.plugin_lines_guidance"\ntype = "int"\ndefault = 2500\n'
        '[[param]]\nname = "budget.max_runtime_dependencies"\ntype = "int"\ndefault = 15\n',
        encoding="utf-8",
    )
    (proj / "plugins").mkdir(parents=True, exist_ok=True)
    return proj


def _create_order_venue_plugin(plugins_dir: Path, poison_sentinel: bool = True) -> Path:
    venue_dir = plugins_dir / "venue" / "fake_order_venue"
    venue_dir.mkdir(parents=True, exist_ok=True)
    (venue_dir / "plugin.toml").write_text(
        'name = "fake_order_venue"\nkind = "venue"\nversion = "1.0.0"\napi_version = 1\n'
        'entry = "plugin:OrderVenuePlugin"\nmodes_allowed = ["live"]\norder_capable = true\n'
        "[capabilities]\norder = true\n",
        encoding="utf-8",
    )
    (venue_dir / "README.md").write_text(
        "# Fake Order Venue\n\n## Summary\nFake order venue plugin for M0 acceptance.\n",
        encoding="utf-8",
    )
    t_dir = venue_dir / "tests"
    t_dir.mkdir(exist_ok=True)
    (t_dir / "test_fake.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    code = (
        "raise RuntimeError('SENTINEL: poison code was executed!')\n"
        if poison_sentinel
        else (
            "from algodesk.api.plugin import PluginBase\n"
            "class OrderVenuePlugin(PluginBase):\n"
            "    pass\n"
        )
    )
    (venue_dir / "plugin.py").write_text(code, encoding="utf-8")
    return venue_dir


# --- 10. Fake order-capable venue plugin ---


@pytest.mark.parametrize("disallowed_mode", [Mode.RESEARCH, Mode.BACKTEST, Mode.PAPER])
def test_fake_order_venue_refused_outside_live(tmp_path: Path, disallowed_mode: Mode) -> None:
    """Item 10: Order-capable plugin is refused in research, backtest, and paper modes."""
    proj = _scaffold_project(tmp_path, f"refuse_{disallowed_mode.value}")
    _create_order_venue_plugin(proj / "plugins", poison_sentinel=True)
    reg = PluginRegistry(proj, [proj / "plugins"], state_dir=proj / ".algodesk")
    ctx = _build_context(disallowed_mode)

    with pytest.raises((SafetyViolation, PluginError)):
        reg.create("venue", "fake_order_venue", ctx=ctx)


@pytest.mark.parametrize("disallowed_mode", [Mode.RESEARCH, Mode.BACKTEST, Mode.PAPER])
def test_fake_order_venue_never_imported_outside_live(
    tmp_path: Path, disallowed_mode: Mode
) -> None:
    """Item 10: Poison code sentinel in order-capable plugin is NEVER executed outside live."""
    proj = _scaffold_project(tmp_path, f"sentinel_{disallowed_mode.value}")
    _create_order_venue_plugin(proj / "plugins", poison_sentinel=True)
    reg = PluginRegistry(proj, [proj / "plugins"], state_dir=proj / ".algodesk")
    ctx = _build_context(disallowed_mode)

    try:
        reg.create("venue", "fake_order_venue", ctx=ctx)
    except (SafetyViolation, PluginError) as exc:
        assert "SENTINEL: poison code was executed" not in str(exc)


def test_fake_order_venue_requires_conformance_record_in_live(tmp_path: Path) -> None:
    """Item 10: Order-capable plugin in LIVE mode is refused without conformance pass."""
    proj = _scaffold_project(tmp_path, "live_unconfirmed")
    _create_order_venue_plugin(proj / "plugins", poison_sentinel=False)
    reg = PluginRegistry(proj, [proj / "plugins"], state_dir=proj / ".algodesk")
    ctx = _build_context(Mode.LIVE)

    with pytest.raises(PluginError, match="conformance record"):
        reg.create("venue", "fake_order_venue", ctx=ctx)


def test_fake_order_venue_accepted_in_live_after_conformance_record(tmp_path: Path) -> None:
    """Item 10: Order-capable plugin is accepted in LIVE after conformance pass is recorded."""
    proj = _scaffold_project(tmp_path, "live_confirmed")
    _create_order_venue_plugin(proj / "plugins", poison_sentinel=False)
    reg = PluginRegistry(proj, [proj / "plugins"], state_dir=proj / ".algodesk")
    ctx = _build_context(Mode.LIVE)

    reg.record_conformance("venue", "fake_order_venue", "m0_acceptance_suite")
    assert reg.conformance_current("venue", "fake_order_venue") is True

    plugin = reg.create("venue", "fake_order_venue", ctx=ctx)
    assert plugin.manifest.name == "fake_order_venue"
    assert plugin.manifest.order_capable is True


def test_editing_plugin_file_invalidates_conformance_record(tmp_path: Path) -> None:
    """Item 10: Editing the plugin file invalidates the conformance record."""
    proj = _scaffold_project(tmp_path, "live_invalidated")
    venue_dir = _create_order_venue_plugin(proj / "plugins", poison_sentinel=False)
    reg = PluginRegistry(proj, [proj / "plugins"], state_dir=proj / ".algodesk")
    ctx = _build_context(Mode.LIVE)

    reg.record_conformance("venue", "fake_order_venue", "m0_acceptance_suite")
    assert reg.conformance_current("venue", "fake_order_venue") is True

    # Mutate plugin.py
    (venue_dir / "plugin.py").write_text(
        "from algodesk.api.plugin import PluginBase\n"
        "class OrderVenuePlugin(PluginBase):\n"
        "    mutated = True\n",
        encoding="utf-8",
    )

    # Conformance is now stale
    reg.refresh()
    assert reg.conformance_current("venue", "fake_order_venue") is False
    with pytest.raises(PluginError, match="conformance record"):
        reg.create("venue", "fake_order_venue", ctx=ctx)


def test_editing_plugin_manifest_invalidates_conformance_record(tmp_path: Path) -> None:
    """Item 10: Editing plugin.toml manifest also invalidates the conformance record."""
    proj = _scaffold_project(tmp_path, "live_manifest_invalidated")
    venue_dir = _create_order_venue_plugin(proj / "plugins", poison_sentinel=False)
    reg = PluginRegistry(proj, [proj / "plugins"], state_dir=proj / ".algodesk")

    reg.record_conformance("venue", "fake_order_venue", "m0_acceptance_suite")
    assert reg.conformance_current("venue", "fake_order_venue") is True

    # Mutate manifest
    manifest_path = venue_dir / "plugin.toml"
    current_content = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(current_content + "\n# comment modification\n", encoding="utf-8")

    reg.refresh()
    assert reg.conformance_current("venue", "fake_order_venue") is False


# --- 11. Journal: write, tamper with raw SQL, verify exit codes ---


def test_journal_untouched_passes_verification(tmp_path: Path) -> None:
    """Item 11: Untouched journal passes verification successfully."""
    db_path = tmp_path / "journal.db"
    settings = JournalSettings(
        flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
    )
    with Journal(db_path, settings=settings) as j:
        j.start_run(
            mode="paper",
            code_version="1.0.0",
            config_hash="cfg_hash",
            registry_hash="reg_hash",
        )
        for i in range(10):
            j.append("trade.intent", {"order_id": f"ord_{i}", "qty": i + 1})

    res = verify_journal(db_path)
    assert res.ok is True
    assert res.checked == 10
    assert res.first_bad_seq is None


def test_journal_tampered_payload_fails_verification(tmp_path: Path) -> None:
    """Item 11: Tampering with row payload using raw SQL causes hash verification failure."""
    db_path = tmp_path / "journal_tampered.db"
    settings = JournalSettings(
        flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
    )
    with Journal(db_path, settings=settings) as j:
        j.start_run(
            mode="paper",
            code_version="1.0.0",
            config_hash="cfg_h",
            registry_hash="reg_h",
        )
        for i in range(5):
            j.append("order.submit", {"seq": i, "price": 100.0 + i})

    # Tamper with raw SQL (drop append-only trigger first to simulate malicious bypass)
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER trg_events_no_update;")
    conn.execute("UPDATE events SET payload_json = '{\"tampered\": true}' WHERE seq = 3;")
    conn.commit()
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is False
    assert res.first_bad_seq == 3
    assert res.reason is not None and "hash mismatch" in res.reason.lower()


def test_journal_tampered_hash_fails_verification(tmp_path: Path) -> None:
    """Item 11: Altering hash column directly also fails verification."""
    db_path = tmp_path / "journal_bad_hash.db"
    settings = JournalSettings(
        flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
    )
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "1.0", "c", "r")
        j.append("test.event", {"k": "v"})

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER trg_events_no_update;")
    conn.execute("UPDATE events SET hash = 'deadbeefdeadbeef' WHERE seq = 1;")
    conn.commit()
    conn.close()

    res = verify_journal(db_path)
    assert res.ok is False
    assert res.first_bad_seq == 1


def test_journal_triggers_forbid_direct_update_and_delete(tmp_path: Path) -> None:
    """Item 11: Journal triggers actively forbid direct SQL UPDATE and DELETE on events."""
    db_path = tmp_path / "journal_triggers.db"
    settings = JournalSettings(
        flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
    )
    with Journal(db_path, settings=settings) as j:
        j.start_run("paper", "1.0", "c", "r")
        j.append("event.one", {"v": 1})

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.DatabaseError, match="UPDATE forbidden"):
        conn.execute("UPDATE events SET kind = 'hacked' WHERE seq = 1;")

    with pytest.raises(sqlite3.DatabaseError, match="DELETE forbidden"):
        conn.execute("DELETE FROM events WHERE seq = 1;")
    conn.close()


def test_journal_cli_verify_untouched_exits_zero(tmp_path: Path) -> None:
    """Item 11: CLI 'algodesk journal verify' exits 0 on untouched journal."""
    proj = _scaffold_project(tmp_path, "p_j_clean")
    with open_runtime(proj, ensure=True) as rt:
        settings = JournalSettings(
            flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
        )
        with Journal(rt.state.journal_db, settings=settings) as j:
            j.start_run("research", "1.0", "c", "r")
            j.append("log.event", {"hello": "world"})

    runner = CliRunner()
    result = runner.invoke(app, ["--root", str(proj), "journal", "verify"])
    assert result.exit_code == 0
    assert "JOURNAL OK" in result.output


def test_journal_cli_verify_tampered_exits_nonzero(tmp_path: Path) -> None:
    """Item 11: CLI 'algodesk journal verify' exits non-zero on tampered journal."""
    proj = _scaffold_project(tmp_path, "p_j_tamper")
    with open_runtime(proj, ensure=True) as rt:
        settings = JournalSettings(
            flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
        )
        with Journal(rt.state.journal_db, settings=settings) as j:
            j.start_run("research", "1.0", "c", "r")
            j.append("evt1", {"idx": 1})
            j.append("evt2", {"idx": 2})

        conn = sqlite3.connect(rt.state.journal_db)
        conn.execute("DROP TRIGGER trg_events_no_update;")
        conn.execute("UPDATE events SET payload_json = '{\"tampered\": 1}' WHERE seq = 2;")
        conn.commit()
        conn.close()

    runner = CliRunner()
    result = runner.invoke(app, ["--root", str(proj), "journal", "verify"])
    assert result.exit_code != 0
    assert "CORRUPT" in result.output or "Hash mismatch" in result.output


# --- 12. Fences: deliberate violations vs clean tree ---


def test_fence_missing_summary_fails_docs_check(tmp_path: Path) -> None:
    """Item 12: Deliberate missing summary makes 'algodesk docs check' exit non-zero."""
    proj = _scaffold_project(tmp_path, "p_no_sum")
    (proj / "docs").mkdir(exist_ok=True)
    (proj / "docs" / "test_doc.md").write_text("# Title Without Summary\n\nBody.\n")

    rep = run_docs_check(proj)
    assert rep.exit_code != 0
    assert any(e.rule == "docs.summary_heading" for e in rep.errors)


def test_fence_plugin_import_violation_fails_lint(tmp_path: Path) -> None:
    """Item 12: Deliberate plugin import violation makes 'algodesk lint' exit non-zero."""
    proj = _scaffold_project(tmp_path, "p_imp_viol")
    pl = proj / "plugins" / "notifier" / "bad_imp"
    pl.mkdir(parents=True)
    (pl / "plugin.toml").write_text(
        'name = "bad_imp"\nkind = "notifier"\nversion = "1.0.0"\n'
        'api_version = 1\nentry = "plugin:BadImp"\nmodes_allowed = ["research"]\n'
        "[capabilities]\nsend = true\n"
    )
    (pl / "plugin.py").write_text(
        "import os\nfrom algodesk.config import loader\nclass BadImp: pass\n"
    )

    rep = run_archlint(proj)
    assert rep.exit_code != 0
    assert any(e.rule == "archlint.plugin_import" for e in rep.errors)


def test_fence_stub_returning_true_fails_lint(tmp_path: Path) -> None:
    """Item 12: Deliberate trivial stub returning True makes 'algodesk lint' exit non-zero."""
    proj = _scaffold_project(tmp_path, "p_stub")
    src = proj / "src" / "algodesk" / "mod"
    src.mkdir(parents=True)
    (src / "service.py").write_text("def check_live() -> bool:\n    return True\n")

    rep = run_archlint(proj)
    assert rep.exit_code != 0
    assert any(e.rule == "archlint.stub_detector" for e in rep.errors)


def test_fence_stub_body_pass_fails_lint(tmp_path: Path) -> None:
    """Item 12: Deliberate stub with only 'pass' body makes 'algodesk lint' exit non-zero."""
    proj = _scaffold_project(tmp_path, "p_stub_pass")
    src = proj / "src" / "algodesk" / "mod"
    src.mkdir(parents=True)
    (src / "handler.py").write_text("def process_event():\n    pass\n")

    rep = run_archlint(proj)
    assert rep.exit_code != 0
    assert any(e.rule == "archlint.stub_detector" for e in rep.errors)


def test_fence_hard_coded_limit_fails_lint(tmp_path: Path) -> None:
    """Item 12: Deliberate hard-coded limit assignment makes 'algodesk lint' exit non-zero."""
    proj = _scaffold_project(tmp_path, "p_hard_limit")
    src = proj / "src" / "algodesk" / "mod"
    src.mkdir(parents=True)
    (src / "limits.py").write_text("max_retry_limit = 5\n")

    rep = run_archlint(proj)
    assert rep.exit_code != 0
    assert any(e.rule == "archlint.hard_coded_limit" for e in rep.errors)


def test_fence_secret_token_in_code_fails_secretscan(tmp_path: Path) -> None:
    """Item 12: Deliberate secret-like string makes 'algodesk secrets scan' exit non-zero."""
    proj = _scaffold_project(tmp_path, "p_secret_exposed")
    src = proj / "src" / "algodesk" / "conn"
    src.mkdir(parents=True)
    token_val = "ghp_" + ("1234567890" * 3) + "123456"
    (src / "client.py").write_text(f'API_TOKEN = "{token_val}"\n')

    rep = run_secretscan(proj)
    assert rep.exit_code != 0
    assert len(rep.findings) > 0


def test_fence_dependency_overflow_fails_budget(tmp_path: Path) -> None:
    """Item 12: Deliberate dependency count overflow makes 'algodesk budget' exit non-zero."""
    proj = _scaffold_project(tmp_path, "p_dep_overflow")
    many_deps = [f'"pkg_{i}"' for i in range(25)]
    deps_str = ", ".join(many_deps)
    (proj / "pyproject.toml").write_text(
        f'[project]\nname = "algodesk"\nversion = "0.0.0"\ndependencies = [{deps_str}]\n'
    )

    rep = run_budget(proj)
    assert rep.exit_code != 0
    assert "FAIL" in rep.summary.status


def test_fence_clean_tree_passes_all_fences(tmp_path: Path) -> None:
    """Item 12: A clean temporary project tree passes all quality fence tools."""
    proj = _scaffold_project(tmp_path, "p_clean_fences")
    src = proj / "src" / "algodesk" / "clean_mod"
    src.mkdir(parents=True)
    (src / "service.py").write_text("def run_job() -> int:\n    return 42\n")

    assert run_docs_check(proj).exit_code == 0
    assert run_archlint(proj).exit_code == 0
    assert run_secretscan(proj).exit_code == 0
    assert run_budget(proj).exit_code == 0


# --- 13. Isolation and doctor ---


def test_isolation_clean_temporary_tree_has_no_critical_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Item 13: In a temporary project tree 'algodesk doctor' has 0 critical results."""
    proj = _scaffold_project(tmp_path, "p_doc_clean")
    fake_py = proj / ".local" / "python" / "bin" / "python"
    fake_py.parent.mkdir(parents=True, exist_ok=True)
    fake_py.touch()
    fake_venv = proj / ".venv"
    fake_venv.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(sys, "executable", str(fake_py))
    monkeypatch.setattr(sys, "prefix", str(fake_venv))
    monkeypatch.setenv("PYTHONNOUSERSITE", "1")
    for v in [
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_BIN_DIR",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "UV_PROJECT_ENVIRONMENT",
    ]:
        monkeypatch.setenv(v, str(proj / ".local" / "uv"))

    with open_runtime(proj, ensure=True):
        pass

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(proj), "doctor", "--json"])
    data = json.loads(res.output)
    assert data["counts"]["critical"] == 0


def test_isolation_escaping_data_path_fails_citing_nfr14(tmp_path: Path) -> None:
    """Item 13: A deliberately escaping data_dir fails configuration with error citing NFR-14."""
    proj = _scaffold_project(tmp_path, "p_esc_data")
    cfg_file = proj / "config" / "platform.toml"
    cfg_file.write_text(
        'project = "algodesk"\nplugin_dirs = ["/tmp/escaped_plugins"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        with open_runtime(proj, ensure=False):
            pass
    assert "NFR-14" in str(exc_info.value)


def test_isolation_traversal_path_fails_citing_nfr14(tmp_path: Path) -> None:
    """Item 13: Path traversal '..' escaping workspace root fails citing NFR-14."""
    proj = _scaffold_project(tmp_path, "p_esc_traversal")
    cfg_file = proj / "config" / "platform.toml"
    cfg_file.write_text(
        'project = "algodesk"\nplugin_dirs = ["../escaped_plugins"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        with open_runtime(proj, ensure=False):
            pass
    assert "NFR-14" in str(exc_info.value)


def test_isolation_doctor_cli_fails_on_escaping_path(tmp_path: Path) -> None:
    """Item 13: Doctor CLI command exits with error citing NFR-14 on escaping path."""
    proj = _scaffold_project(tmp_path, "p_esc_cli")
    cfg_file = proj / "config" / "platform.toml"
    cfg_file.write_text(
        'project = "algodesk"\nplugin_dirs = ["../escaped_plugins"]\n',
        encoding="utf-8",
    )

    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(proj), "doctor"])
    assert res.exit_code != 0
    assert "NFR-14" in res.output


# --- 14. Parameters: tighten, non-live loosen, armed live loosen ---


def test_param_tighten_applies_immediately_with_history(tmp_path: Path) -> None:
    """Item 14: Tightening parameter applies immediately and creates history row."""
    db_file = tmp_path / "param_tighten.db"
    reg = ParamRegistry(db_file, mode=Mode.PAPER)
    spec = ParamSpec(
        name="risk.limit.daily",
        type=ParamType.FLOAT,
        default=2.0,
        min=0.1,
        max=10.0,
        tighten=Tighten.DOWN,
    )
    reg.register(spec)

    # Tighten: 2.0 -> 1.0 (down)
    res = reg.set("risk.limit.daily", 1.0, by="trader1", reason="tighten for risk")
    assert res.direction == "tighten"
    assert res.status == "applied"
    assert res.effective_at == "now"
    assert reg.get("risk.limit.daily") == 1.0

    # History row
    hist = reg.history("risk.limit.daily")
    assert len(hist) == 1
    assert hist[0].old_value == 2.0
    assert hist[0].new_value == 1.0
    assert hist[0].by == "trader1"
    assert hist[0].reason == "tighten for risk"
    assert hist[0].status == "applied"


def test_param_non_live_loosen_applies_immediately_with_history(tmp_path: Path) -> None:
    """Item 14: Loosening in non-live mode applies immediately and creates history row."""
    db_file = tmp_path / "param_loosen_paper.db"
    reg = ParamRegistry(db_file, mode=Mode.PAPER)
    spec = ParamSpec(
        name="risk.limit.daily",
        type=ParamType.FLOAT,
        default=2.0,
        min=0.1,
        max=10.0,
        tighten=Tighten.DOWN,
    )
    reg.register(spec)

    # Loosen: 2.0 -> 3.0 (up, where tighten is down)
    res = reg.set("risk.limit.daily", 3.0, by="trader2", reason="relaxing for backtest")
    assert res.direction == "loosen"
    assert res.status == "applied"
    assert res.effective_at == "now"
    assert reg.get("risk.limit.daily") == 3.0

    hist = reg.history("risk.limit.daily")
    assert len(hist) == 1
    assert hist[0].new_value == 3.0
    assert hist[0].status == "applied"


def test_param_live_armed_loosen_pending_with_confirmation(tmp_path: Path) -> None:
    """Item 14: Pending live loosen with registry armed stages pending and leaves history."""
    db_file = tmp_path / "param_armed_live.db"
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

    # Missing confirm rejected
    with pytest.raises(ParamError, match="requires typed confirmation"):
        reg.set("risk.limit.daily", 3.0, by="trader_live", reason="relax limit")

    # Wrong confirm rejected
    with pytest.raises(ParamError, match="requires typed confirmation"):
        reg.set(
            "risk.limit.daily",
            3.0,
            by="trader_live",
            reason="relax limit",
            confirm="LOOSEN WRONG",
        )

    # Correct confirm stages pending
    res = reg.set(
        "risk.limit.daily",
        3.0,
        by="trader_live",
        reason="relax limit live",
        confirm="LOOSEN risk.limit.daily",
    )
    assert res.direction == "loosen"
    assert res.status == "pending"
    assert res.effective_at == "session_start"

    # Effective value is STILL unchanged until session_start
    assert reg.get("risk.limit.daily") == 2.0

    hist = reg.history("risk.limit.daily")
    assert len(hist) == 1
    assert hist[0].status == "pending"
    assert hist[0].confirmed == "LOOSEN risk.limit.daily"


def test_param_live_armed_tighten_is_immediate_without_confirm(tmp_path: Path) -> None:
    """Item 14: Even in live and armed mode, tightening applies immediately without confirmation."""
    db_file = tmp_path / "param_live_tighten.db"
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

    res = reg.set("risk.limit.daily", 1.5, by="trader_live", reason="emergency tighten")
    assert res.direction == "tighten"
    assert res.status == "applied"
    assert res.effective_at == "now"
    assert reg.get("risk.limit.daily") == 1.5


# --- CLI End-to-End Workflow Commands ---


def test_cli_version_end_to_end() -> None:
    """End-to-end version command returns success and version tag."""
    runner = CliRunner()
    res = runner.invoke(app, ["version"])
    assert res.exit_code == 0
    assert "algodesk" in res.output


def test_cli_config_show_effective_end_to_end(tmp_path: Path) -> None:
    """End-to-end config show command displays effective config and hash."""
    proj = _scaffold_project(tmp_path, "p_cfg_cli")
    runner = CliRunner()
    res = runner.invoke(app, ["--root", str(proj), "config", "show"])
    assert res.exit_code == 0
    assert "EFFECTIVE CONFIG" in res.output
    assert "sha256:" in res.output


def test_cli_journal_runs_and_tail_end_to_end(tmp_path: Path) -> None:
    """End-to-end journal runs and tail inspection."""
    proj = _scaffold_project(tmp_path, "p_j_runs")
    with open_runtime(proj, ensure=True) as rt:
        settings = JournalSettings(
            flush_interval_ms=10, max_buffer_events=100, backpressure_timeout_ms=1000
        )
        with Journal(rt.state.journal_db, settings=settings) as j:
            run_id = j.start_run("research", "1.0", "cfg", "reg")
            j.append("tick.data", {"sym": "NIFTY", "px": 25000})

    runner = CliRunner()
    runs_res = runner.invoke(app, ["--root", str(proj), "journal", "runs"])
    assert runs_res.exit_code == 0
    assert run_id in runs_res.output

    tail_res = runner.invoke(app, ["--root", str(proj), "journal", "tail", "-n", "5"])
    assert tail_res.exit_code == 0
    assert "tick.data" in tail_res.output
