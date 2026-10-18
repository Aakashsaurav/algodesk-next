"""Base plugin conformance checks and doctor diagnostics."""

from __future__ import annotations

from typing import Any

from algodesk.api.checks import CheckResult, DoctorCheck, Severity
from algodesk.api.errors import NotSupported
from algodesk.api.kinds import Mode
from algodesk.api.manifest import PluginManifest
from algodesk.registry.registry import PluginRegistry


def base_contract_problems(plugin: Any) -> list[str]:
    """Verify that a plugin instance adheres to the base plugin contract.

    Returns an empty list if conforming, or a list of problem descriptions.
    """
    problems: list[str] = []

    if not hasattr(plugin, "manifest") or not isinstance(plugin.manifest, PluginManifest):
        problems.append("Plugin must have a 'manifest' attribute of type PluginManifest")
        return problems

    manifest: PluginManifest = plugin.manifest

    for cap, enabled in manifest.capabilities.items():
        if not hasattr(plugin, "supports"):
            problems.append("Plugin has no 'supports' method")
            break
        try:
            supported = plugin.supports(cap)
            if supported != enabled:
                problems.append(
                    f"supports('{cap}') returned {supported}, expected {enabled}",
                )
        except Exception as exc:
            problems.append(f"supports('{cap}') raised an exception: {exc}")

    if not hasattr(plugin, "doctor_checks"):
        problems.append("Plugin has no 'doctor_checks' method")
    else:
        try:
            checks = plugin.doctor_checks()
            seen_ids: set[str] = set()
            for c in checks:
                if not isinstance(c, DoctorCheck):
                    problems.append(
                        f"Item returned by doctor_checks() is not a DoctorCheck: {type(c)}",
                    )
                elif c.id in seen_ids:
                    problems.append(f"Duplicate doctor check id: '{c.id}'")
                else:
                    seen_ids.add(c.id)
        except Exception as exc:
            problems.append(f"doctor_checks() raised an exception: {exc}")

    if not hasattr(plugin, "require"):
        problems.append("Plugin has no 'require' method")
    else:
        test_cap = "__algodesk_undeclared_test_capability__"
        try:
            plugin.require(test_cap)
            problems.append("require() did not raise NotSupported for undeclared capability")
        except NotSupported:
            pass
        except Exception as exc:
            problems.append(
                f"require() raised {type(exc).__name__} instead of NotSupported: {exc}",
            )

    return problems


def registry_doctor_checks(registry: PluginRegistry) -> list[DoctorCheck]:
    """Return diagnostic doctor checks for the plugin registry."""

    def check_manifests_valid() -> CheckResult:
        registry.refresh()
        issues = registry.issues
        if not issues:
            return CheckResult(
                ok=True,
                message="All discovered plugin manifests and structures are valid",
            )
        first = issues[0]
        return CheckResult(
            ok=False,
            message=f"{len(issues)} plugin issue(s) detected; first: {first.what}",
            fix=first.fix,
        )

    def check_no_order_capable_outside_live() -> CheckResult:
        for kind, name, mode in registry.loaded():
            p_info = registry.info(kind, name)
            if p_info.manifest.order_capable and mode != Mode.LIVE:
                return CheckResult(
                    ok=False,
                    message=(
                        f"Order-capable plugin '{kind}/{name}' was loaded in mode '{mode.value}'"
                    ),
                    fix="Unload order-capable plugin or switch to live mode",
                )
        return CheckResult(
            ok=True,
            message="No order-capable plugins loaded outside live mode",
        )

    return [
        DoctorCheck(
            id="plugins.manifests_valid",
            severity=Severity.WARNING,
            run=check_manifests_valid,
        ),
        DoctorCheck(
            id="plugins.no_order_capable_outside_live",
            severity=Severity.CRITICAL,
            run=check_no_order_capable_outside_live,
        ),
    ]
