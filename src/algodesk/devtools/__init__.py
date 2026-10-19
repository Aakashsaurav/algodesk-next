"""AlgoDesk development and diagnostic quality tools."""

from __future__ import annotations

from typing import Any

from algodesk.api.checks import DoctorCheck


def build_tool_checks(runtime: Any) -> list[DoctorCheck]:
    """Build doctor diagnostic checks for devtools fences."""
    from algodesk.devtools.archlint import make_archlint_check
    from algodesk.devtools.budget import make_budget_checks
    from algodesk.devtools.docs import make_docs_check
    from algodesk.devtools.secretscan import make_secretscan_check

    checks: list[DoctorCheck] = []
    checks.extend(make_budget_checks(runtime))
    checks.append(make_docs_check(runtime))
    checks.append(make_archlint_check(runtime))
    checks.append(make_secretscan_check(runtime))
    return checks


__all__ = ["build_tool_checks"]
