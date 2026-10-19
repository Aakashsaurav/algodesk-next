"""Tests for PluginRegistry creation, gating, and safe lazy loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def test_registry_list_and_info(tmp_path: Path, fake_plugin_factory: Any) -> None:
    from algodesk.api.kinds import PluginKind
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(plugins_dir, "notifier", "p1")
    fake_plugin_factory(plugins_dir, "venue", "p2")

    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")
    assert len(reg.list()) == 2
    assert len(reg.list(PluginKind.NOTIFIER)) == 1
    assert len(reg.list("venue")) == 1
    assert len(reg.list("broker")) == 0

    info = reg.info("notifier", "p1")
    assert info.name == "p1"
    assert info.kind == PluginKind.NOTIFIER


def test_registry_info_not_found(tmp_path: Path) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.registry.registry import PluginRegistry

    reg = PluginRegistry(tmp_path, [tmp_path / "plugins"], state_dir=tmp_path / ".algodesk")
    with pytest.raises(PluginError, match="not found"):
        reg.info("notifier", "nonexistent")


def test_create_success_without_schema(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(plugins_dir, "notifier", "simple")
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    plugin = reg.create("notifier", "simple", ctx=ctx)
    assert plugin.manifest.name == "simple"
    assert reg.loaded() == [(plugin.manifest.kind, "simple", Mode.RESEARCH)]


def test_create_success_with_schema(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    schema_code = (
        "from pydantic import BaseModel\n\n"
        "class ConfigSchema(BaseModel):\n"
        "    api_token: str\n"
        "    timeout: int = 30\n"
    )
    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "with_schema",
        config_schema="schema.py",
        extra_files={"schema.py": schema_code},
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    cfg = {"api_token": "secret_abc", "timeout": 10}
    plugin = reg.create("notifier", "with_schema", config=cfg, ctx=ctx)
    assert plugin.manifest.name == "with_schema"


def test_create_config_schema_validation_error(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.errors import ConfigError
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    schema_code = (
        "from pydantic import BaseModel\n\n"
        "class ConfigSchema(BaseModel):\n"
        "    api_token: str\n"
        "    retries: int\n"
    )
    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "bad_cfg",
        config_schema="schema.py",
        extra_files={"schema.py": schema_code},
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    # Missing 'retries' key
    with pytest.raises(ConfigError) as exc_info:
        reg.create("notifier", "bad_cfg", config={"api_token": "tok"}, ctx=ctx)
    assert "retries" in exc_info.value.what


def test_create_no_schema_with_options_rejected(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(plugins_dir, "notifier", "no_schema")
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    with pytest.raises(PluginError, match="declares no config_schema"):
        reg.create("notifier", "no_schema", config={"unexpected_opt": 123}, ctx=ctx)


def test_create_mode_not_allowed(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "research_only",
        modes_allowed=["research"],
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.BACKTEST)
    with pytest.raises(PluginError, match="cannot run in mode 'backtest'"):
        reg.create("notifier", "research_only", ctx=ctx)


@pytest.mark.parametrize("disallowed_mode", ["research", "backtest", "paper"])
def test_order_capable_refused_outside_live_without_import(
    disallowed_mode: str,
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.errors import SafetyViolation
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    poison_code = (
        "raise RuntimeError('CRITICAL: Order capable plugin was imported in non-live mode!')\n"
    )
    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "venue",
        "order_plugin",
        order_capable=True,
        modes_allowed=["live"],
        plugin_py_content=poison_code,
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode(disallowed_mode))
    with pytest.raises((SafetyViolation, Exception)) as exc_info:
        reg.create("venue", "order_plugin", ctx=ctx)

    # Must raise SafetyViolation (or PluginError for mode disallowed), and poison must NOT have run!
    assert "CRITICAL: Order capable plugin was imported" not in str(exc_info.value)


def test_order_capable_accepted_in_live_with_conformance(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "venue",
        "live_venue",
        order_capable=True,
        modes_allowed=["live"],
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")
    ctx = context_factory(Mode.LIVE)

    # Without conformance pass, create in live fails with PluginError
    with pytest.raises(Exception, match="conformance record"):
        reg.create("venue", "live_venue", ctx=ctx)

    # Record conformance pass
    reg.record_conformance("venue", "live_venue", "acceptance_suite")
    assert reg.conformance_current("venue", "live_venue") is True

    # Now create in live succeeds
    plugin = reg.create("venue", "live_venue", ctx=ctx)
    assert plugin.manifest.name == "live_venue"


def test_paper_mode_requires_conformance(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(plugins_dir, "notifier", "paper_plugin")
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")
    ctx = context_factory(Mode.PAPER)

    # Without conformance pass, paper fails
    with pytest.raises(Exception, match="conformance record"):
        reg.create("notifier", "paper_plugin", ctx=ctx)

    # Record conformance pass
    reg.record_conformance("notifier", "paper_plugin", "suite")
    plugin = reg.create("notifier", "paper_plugin", ctx=ctx)
    assert plugin.manifest.name == "paper_plugin"


def test_research_and_backtest_do_not_require_conformance(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(plugins_dir, "notifier", "easy_plugin")
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    # Research works without conformance
    ctx_res = context_factory(Mode.RESEARCH)
    p1 = reg.create("notifier", "easy_plugin", ctx=ctx_res)
    assert p1.manifest.name == "easy_plugin"

    # Backtest works without conformance
    ctx_bt = context_factory(Mode.BACKTEST)
    p2 = reg.create("notifier", "easy_plugin", ctx=ctx_bt)
    assert p2.manifest.name == "easy_plugin"


def test_create_entry_class_missing(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    # Entry names NonExistentClass, but plugin.py has OtherClass
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "missing_cls",
        entry="plugin:NonExistentClass",
        plugin_py_content="class OtherClass: pass\n",
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    with pytest.raises(PluginError, match="Entry class 'NonExistentClass' not found") as exc_info:
        reg.create("notifier", "missing_cls", ctx=ctx)
    err = exc_info.value
    assert "NonExistentClass" in err.what
    assert "plugin" in err.what
    assert "plugin.py" in err.what
    assert "NonExistentClass" in err.fix
    assert "plugin" in err.fix
    assert "plugin.py" in err.fix


def test_create_wrong_constructor(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    code = "class BadConstructor:\n    def __init__(self, only_one_arg):\n        pass\n"
    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "bad_init",
        entry="plugin:BadConstructor",
        plugin_py_content=code,
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    with pytest.raises(PluginError, match="Failed to instantiate"):
        reg.create("notifier", "bad_init", ctx=ctx)


def test_create_object_not_a_plugin(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    # Object has valid constructor, but does not implement Plugin protocol
    # (no supports / doctor_checks methods)
    code = (
        "class NotAPlugin:\n"
        "    def __init__(self, manifest, config, ctx):\n"
        "        self.manifest = manifest\n"
    )
    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "not_plugin",
        entry="plugin:NotAPlugin",
        plugin_py_content=code,
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    with pytest.raises(PluginError, match="does not implement Plugin protocol"):
        reg.create("notifier", "not_plugin", ctx=ctx)


def test_create_manifest_mismatch(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    code = (
        "from algodesk.api.plugin import PluginBase\n"
        "class MutatedManifest(PluginBase):\n"
        "    def __init__(self, manifest, config, ctx):\n"
        "        super().__init__(manifest, config, ctx)\n"
        "        self.manifest = None\n"
    )
    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "mut_manifest",
        entry="plugin:MutatedManifest",
        plugin_py_content=code,
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    with pytest.raises(PluginError, match="manifest mismatch"):
        reg.create("notifier", "mut_manifest", ctx=ctx)


def test_create_import_error_wrapped(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    code = "import nonexistent_package_that_does_not_exist_xyz\n"
    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "import_broken",
        plugin_py_content=code,
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    with pytest.raises(PluginError, match="Failed to import plugin entry module"):
        reg.create("notifier", "import_broken", ctx=ctx)


def test_create_constructor_error_does_not_leak_config(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.errors import PluginError
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    schema_code = (
        "from pydantic import BaseModel\nclass ConfigSchema(BaseModel):\n    api_key: str\n"
    )
    code = (
        "from algodesk.api.plugin import PluginBase\n"
        "class ExplodingPlugin(PluginBase):\n"
        "    def __init__(self, manifest, config, ctx):\n"
        "        raise RuntimeError('boom!')\n"
    )
    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "exploding",
        config_schema="schema.py",
        plugin_py_content=code,
        extra_files={"schema.py": schema_code},
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    secret_val = "SECRET_SUPER_SENSITIVE_KEY_12345"
    ctx = context_factory(Mode.RESEARCH)
    with pytest.raises(PluginError) as exc_info:
        reg.create("notifier", "exploding", config={"api_key": secret_val}, ctx=ctx)
    assert secret_val not in str(exc_info.value)
    assert secret_val not in exc_info.value.what


def test_relative_import_inside_plugin(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    helper_code = "def get_greeting(): return 'hello from helper'\n"
    plugin_code = (
        "from algodesk.api.plugin import PluginBase\n"
        "from .helper import get_greeting\n"
        "class RelativePlugin(PluginBase):\n"
        "    def greet(self):\n"
        "        return get_greeting()\n"
    )
    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "rel_test",
        entry="plugin:RelativePlugin",
        plugin_py_content=plugin_code,
        extra_files={"helper.py": helper_code},
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    plugin = reg.create("notifier", "rel_test", ctx=ctx)
    assert hasattr(plugin, "greet")
    assert plugin.greet() == "hello from helper"  # type: ignore[attr-defined]


def test_two_plugins_same_filename_do_not_collide(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    p1_code = (
        "from algodesk.api.plugin import PluginBase\n"
        "class MyPlugin(PluginBase):\n"
        "    tag = 'plugin1'\n"
    )
    p2_code = (
        "from algodesk.api.plugin import PluginBase\n"
        "class MyPlugin(PluginBase):\n"
        "    tag = 'plugin2'\n"
    )
    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "inst1",
        entry="plugin:MyPlugin",
        plugin_py_content=p1_code,
    )
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "inst2",
        entry="plugin:MyPlugin",
        plugin_py_content=p2_code,
    )
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    ctx = context_factory(Mode.RESEARCH)
    plugin1 = reg.create("notifier", "inst1", ctx=ctx)
    plugin2 = reg.create("notifier", "inst2", ctx=ctx)

    assert plugin1.tag == "plugin1"  # type: ignore[attr-defined]
    assert plugin2.tag == "plugin2"  # type: ignore[attr-defined]


def test_loaded_returns_all_created_plugins(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(plugins_dir, "notifier", "load1")
    fake_plugin_factory(plugins_dir, "notifier", "load2")
    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")

    assert reg.loaded() == []
    ctx = context_factory(Mode.RESEARCH)
    reg.create("notifier", "load1", ctx=ctx)
    reg.create("notifier", "load2", ctx=ctx)

    loaded = reg.loaded()
    assert len(loaded) == 2
    assert (loaded[0][0].value, loaded[0][1], loaded[0][2]) == ("notifier", "load1", Mode.RESEARCH)
    assert (loaded[1][0].value, loaded[1][1], loaded[1][2]) == ("notifier", "load2", Mode.RESEARCH)


def test_create_preserves_sys_path_and_module_naming_on_success_and_failure(
    tmp_path: Path,
    fake_plugin_factory: Any,
    context_factory: Any,
) -> None:
    import sys

    from algodesk.api.errors import PluginError
    from algodesk.api.kinds import Mode
    from algodesk.registry.registry import PluginRegistry

    plugins_dir = tmp_path / "plugins"
    fake_plugin_factory(plugins_dir, "notifier", "good_plugin")
    fake_plugin_factory(
        plugins_dir,
        "notifier",
        "bad_plugin",
        entry="plugin:BrokenClass",
        plugin_py_content="raise RuntimeError('Explosion on import')\n",
    )

    reg = PluginRegistry(tmp_path, [plugins_dir], state_dir=tmp_path / ".algodesk")
    ctx = context_factory(Mode.RESEARCH)

    original_sys_path = list(sys.path)

    # 1. Successful load
    plugin = reg.create("notifier", "good_plugin", ctx=ctx)
    assert plugin.manifest.name == "good_plugin"
    assert sys.path == original_sys_path

    expected_mod_name = "_algodesk_plugins_notifier_good_plugin.plugin"
    assert expected_mod_name in sys.modules
    assert sys.modules[expected_mod_name].__name__ == expected_mod_name

    # 2. Failed load
    with pytest.raises(PluginError, match="Explosion on import"):
        reg.create("notifier", "bad_plugin", ctx=ctx)

    assert sys.path == original_sys_path
    bad_mod_name = "_algodesk_plugins_notifier_bad_plugin.plugin"
    assert bad_mod_name not in sys.modules
