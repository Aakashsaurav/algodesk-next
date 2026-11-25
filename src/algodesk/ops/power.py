"""Power, battery, and sleep-prevention helpers for macOS sessions."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from algodesk.api.checks import CheckResult, Severity

if TYPE_CHECKING:
    from algodesk.ops.runtime import Runtime

_BATT_PCT_RE = re.compile(r"(\d+)%")
_ASSERT_SUMMARY_RE = re.compile(r"PreventUserIdleSystemSleep\s+(\d+)")


def parse_pmset_batt(text: str) -> tuple[bool, bool, int | None]:
    """Parse ``pmset -g batt`` output into ``(on_ac, on_battery, battery_pct)``."""
    on_ac = "AC Power" in text
    on_battery = "Battery Power" in text
    m = _BATT_PCT_RE.search(text)
    battery_pct = int(m.group(1)) if m else None
    return on_ac, on_battery, battery_pct


def parse_pmset_assertions(text: str) -> bool:
    """Return True if ``pmset -g assertions`` shows an active PreventUserIdleSystemSleep."""
    m = _ASSERT_SUMMARY_RE.search(text)
    if m is not None:
        return int(m.group(1)) > 0
    for line in text.splitlines():
        if "PreventUserIdleSystemSleep" in line and "named:" in line:
            return True
    return False


def evaluate_power_check(
    platform: str,
    batt_text: str,
    assertions_text: str,
    *,
    min_battery_pct: int,
    require_ac: bool = False,
    keep_awake: bool = True,
) -> CheckResult:
    """Evaluate macOS power and sleep-assertion state from captured pmset outputs."""
    if platform != "darwin":
        return CheckResult(
            ok=True,
            message=f"Power check skipped on {platform} (macOS only)",
        )

    on_ac, on_battery, pct = parse_pmset_batt(batt_text)
    prevent_sleep = parse_pmset_assertions(assertions_text)

    if on_battery or not on_ac:
        pct_str = f"{pct}%" if pct is not None else "unknown%"
        if pct is not None and pct < min_battery_pct:
            return CheckResult(
                ok=False,
                severity=Severity.CRITICAL,
                message=f"on battery ({pct_str}), below minimum {min_battery_pct}%",
                fix="plug in the charger",
            )
        if require_ac:
            return CheckResult(
                ok=False,
                severity=Severity.CRITICAL,
                message=f"on battery ({pct_str}) and ops.require_ac is true",
                fix="plug in the charger",
            )
        return CheckResult(
            ok=False,
            severity=Severity.WARNING,
            message=f"on battery ({pct_str})",
            fix="plug in the charger",
        )

    if not prevent_sleep and not keep_awake:
        return CheckResult(
            ok=False,
            severity=Severity.WARNING,
            message="No sleep-prevention assertion held and ops.keep_awake is false",
            fix="run with caffeinate -i",
        )

    sleep_desc = "sleep-prevention active" if prevent_sleep else "ops.keep_awake=true"
    return CheckResult(ok=True, message=f"AC Power ({sleep_desc})")


def _read_ops_param(runtime: Runtime, key: str, default: Any) -> Any:
    if runtime.params is not None:
        try:
            val = runtime.params.get(key)
            if val is not None:
                return val
        except Exception:
            pass
    return default


def check_power(
    runtime: Runtime,
    *,
    run_cmd: Callable[..., Any] = subprocess.run,
    platform: str | None = None,
) -> CheckResult:
    """Run ``pmset -g batt`` and ``pmset -g assertions`` without a shell and evaluate."""
    plat = platform if platform is not None else sys.platform
    if plat != "darwin":
        return CheckResult(
            ok=True,
            message=f"Power check skipped on {plat} (macOS only)",
        )
    if os.environ.get("PYTEST_CURRENT_TEST") and run_cmd is subprocess.run:
        return CheckResult(
            ok=True,
            message="Power check skipped during pytest (use run_cmd fixture)",
        )

    min_batt = int(_read_ops_param(runtime, "ops.min_battery_pct", 50))
    req_ac = bool(_read_ops_param(runtime, "ops.require_ac", False))
    keep_awake = bool(_read_ops_param(runtime, "ops.keep_awake", True))

    pmset_timeout = float(_read_ops_param(runtime, "ops.pmset_timeout_s", None) or 5.0)
    try:
        batt_proc = run_cmd(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=pmset_timeout,
        )
        assert_proc = run_cmd(
            ["pmset", "-g", "assertions"],
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=pmset_timeout,
        )
    except Exception as exc:
        return CheckResult(
            ok=False,
            severity=Severity.WARNING,
            message=f"Failed to query pmset: {exc}",
            fix="Verify pmset is available on macOS",
        )

    return evaluate_power_check(
        plat,
        str(getattr(batt_proc, "stdout", "") or ""),
        str(getattr(assert_proc, "stdout", "") or ""),
        min_battery_pct=min_batt,
        require_ac=req_ac,
        keep_awake=keep_awake,
    )


def start_keep_awake(
    pid: int,
    *,
    popen_fn: Callable[..., Any] = subprocess.Popen,
    platform: str | None = None,
) -> tuple[Any | None, str | None]:
    """Start ``caffeinate -i -w <pid>`` on macOS without a shell; return ``(proc, error)``."""
    plat = platform if platform is not None else sys.platform
    if plat != "darwin":
        return None, None
    if os.environ.get("PYTEST_CURRENT_TEST") and popen_fn is subprocess.Popen:
        return None, None
    try:
        proc = popen_fn(
            ["caffeinate", "-i", "-w", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc, None
    except Exception as exc:
        return None, str(exc)


def stop_keep_awake(proc: Any | None, *, wait_seconds: float | None = None) -> None:
    """Terminate a spawned caffeinate process cleanly if still running."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            wait_s = wait_seconds if wait_seconds is not None else 2.0
            proc.wait(timeout=wait_s)
    except Exception:
        pass
