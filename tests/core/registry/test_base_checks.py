"""Tests for base plugin contract verification."""

from __future__ import annotations

from typing import Any

from algodesk.api.checks import CheckResult, DoctorCheck, Severity
from algodesk.api.errors import NotSupported
from algodesk.api.kinds import PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.plugin import PluginBase


def _make_manifest(caps: dict[str, bool] | None = None) -> PluginManifest:
    return PluginManifest(
        name="test_plugin",
        kind=PluginKind.NOTIFIER,
        version="0.1.0",
        api_version=1,
        entry="plugin:TestPlugin",
        capabilities=caps or {"send": True, "receive": False},
    )


class ConformingPlugin(PluginBase):
    pass


def test_base_contract_problems_conforming() -> None:
    from algodesk.registry.base_checks import base_contract_problems

    manifest = _make_manifest()
    plugin = ConformingPlugin(manifest, {}, None)  # type: ignore[arg-type]
    problems = base_contract_problems(plugin)
    assert problems == []


def test_base_contract_problems_missing_manifest() -> None:
    from algodesk.registry.base_checks import base_contract_problems

    class NoManifest:
        pass

    problems = base_contract_problems(NoManifest())
    assert any("Plugin must have a 'manifest'" in p for p in problems)


def test_base_contract_problems_not_manifest_instance() -> None:
    from algodesk.registry.base_checks import base_contract_problems

    class FakeManifestPlugin:
        manifest = "not a manifest object"

    problems = base_contract_problems(FakeManifestPlugin())
    assert any(
        "Plugin must have a 'manifest' attribute of type PluginManifest" in p for p in problems
    )


def test_base_contract_problems_supports_disagreement() -> None:
    from algodesk.registry.base_checks import base_contract_problems

    manifest = _make_manifest({"send": True, "receive": False})

    class DisagreeingPlugin(PluginBase):
        def supports(self, capability: str) -> bool:
            return False

    plugin = DisagreeingPlugin(manifest, {}, None)  # type: ignore[arg-type]
    problems = base_contract_problems(plugin)
    assert any("supports('send') returned False, expected True" in p for p in problems)


def test_base_contract_problems_supports_exception() -> None:
    from algodesk.registry.base_checks import base_contract_problems

    manifest = _make_manifest()

    class ExplodingSupports(PluginBase):
        def supports(self, capability: str) -> bool:
            raise RuntimeError("crash")

    plugin = ExplodingSupports(manifest, {}, None)  # type: ignore[arg-type]
    problems = base_contract_problems(plugin)
    assert any("supports('send') raised an exception" in p for p in problems)


def test_base_contract_problems_no_doctor_checks() -> None:
    from algodesk.registry.base_checks import base_contract_problems

    manifest = _make_manifest()

    class NoDoctorChecks:
        def __init__(self, m: Any) -> None:
            self.manifest = m

        def supports(self, cap: str) -> bool:
            return True

        def require(self, cap: str) -> None:
            raise NotSupported("x", "y", "z")

    plugin = NoDoctorChecks(manifest)
    problems = base_contract_problems(plugin)
    assert any("Plugin has no 'doctor_checks' method" in p for p in problems)


def test_base_contract_problems_invalid_doctor_check_item() -> None:
    from algodesk.registry.base_checks import base_contract_problems

    manifest = _make_manifest()

    class BadDoctorItem(PluginBase):
        def doctor_checks(self) -> Any:
            return ["not a doctor check"]

    plugin = BadDoctorItem(manifest, {}, None)  # type: ignore[arg-type]
    problems = base_contract_problems(plugin)
    assert any("Item returned by doctor_checks() is not a DoctorCheck" in p for p in problems)


def test_base_contract_problems_duplicate_doctor_check_ids() -> None:
    from algodesk.registry.base_checks import base_contract_problems

    manifest = _make_manifest()

    class DupDoctorChecks(PluginBase):
        def doctor_checks(self) -> Any:
            c1 = DoctorCheck(
                id="dup.check",
                severity=Severity.WARNING,
                run=lambda: CheckResult(ok=True, message="ok"),
            )
            c2 = DoctorCheck(
                id="dup.check",
                severity=Severity.WARNING,
                run=lambda: CheckResult(ok=True, message="ok"),
            )
            return [c1, c2]

    plugin = DupDoctorChecks(manifest, {}, None)  # type: ignore[arg-type]
    problems = base_contract_problems(plugin)
    assert any("Duplicate doctor check id: 'dup.check'" in p for p in problems)


def test_base_contract_problems_doctor_checks_exception() -> None:
    from algodesk.registry.base_checks import base_contract_problems

    manifest = _make_manifest()

    class CrashDoctor(PluginBase):
        def doctor_checks(self) -> Any:
            raise RuntimeError("crash doctor")

    plugin = CrashDoctor(manifest, {}, None)  # type: ignore[arg-type]
    problems = base_contract_problems(plugin)
    assert any("doctor_checks() raised an exception" in p for p in problems)


def test_base_contract_problems_require_does_not_raise() -> None:
    from algodesk.registry.base_checks import base_contract_problems

    manifest = _make_manifest()

    class SilentRequire(PluginBase):
        def require(self, capability: str) -> None:
            pass

    plugin = SilentRequire(manifest, {}, None)  # type: ignore[arg-type]
    problems = base_contract_problems(plugin)
    assert any("require() did not raise NotSupported" in p for p in problems)


def test_base_contract_problems_require_raises_wrong_exception() -> None:
    from algodesk.registry.base_checks import base_contract_problems

    manifest = _make_manifest()

    class WrongExceptionRequire(PluginBase):
        def require(self, capability: str) -> None:
            raise KeyError(capability)

    plugin = WrongExceptionRequire(manifest, {}, None)  # type: ignore[arg-type]
    problems = base_contract_problems(plugin)
    assert any("require() raised KeyError instead of NotSupported" in p for p in problems)
