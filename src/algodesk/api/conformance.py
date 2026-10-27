"""Pure conformance verification helpers per plugin kind.

No test runner or pytest imports. Returns a list of problem strings.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from algodesk.api.errors import CalendarCoverageError, ConfigError
from algodesk.api.protocols import Message


def _check_methods(obj: Any, methods: list[str], kind_name: str) -> list[str]:
    problems: list[str] = []
    for m in methods:
        attr = getattr(obj, m, None)
        if attr is None or not callable(attr):
            problems.append(f"{kind_name} must implement callable '{m}()'")
    return problems


def data_source_problems(obj: Any) -> list[str]:
    """Verify DataSource / broker protocol conformance."""
    return _check_methods(obj, ["instruments", "history", "stream"], "DataSource")


def venue_problems(obj: Any) -> list[str]:
    """Verify ExecutionVenue protocol conformance."""
    problems = _check_methods(
        obj,
        ["place", "cancel", "on_quote", "events", "positions", "funds"],
        "ExecutionVenue",
    )
    # Check that place and cancel are coroutine functions
    place_fn = getattr(obj, "place", None)
    if place_fn is not None and not inspect.iscoroutinefunction(place_fn):
        problems.append("ExecutionVenue.place() must be an async coroutine function")
    cancel_fn = getattr(obj, "cancel", None)
    if cancel_fn is not None and not inspect.iscoroutinefunction(cancel_fn):
        problems.append("ExecutionVenue.cancel() must be an async coroutine function")
    return problems


def indicator_problems(obj: Any, golden: Mapping[str, Any] | None = None) -> list[str]:
    """Verify Indicator protocol conformance and batch/incremental equality."""
    problems: list[str] = []
    if not hasattr(obj, "spec"):
        problems.append("Indicator must define a 'spec' attribute of type IndicatorSpec")

    problems.extend(_check_methods(obj, ["compute", "new_state", "update"], "Indicator"))
    if problems:
        return problems

    test_values: Sequence[float] = [10.0, 11.5, 12.0, 11.0, 13.0, 14.5, 13.5, 15.0]
    expected: Sequence[float | None] | None = None
    if golden is not None:
        if "inputs" in golden:
            test_values = golden["inputs"]
        elif "values" in golden:
            test_values = golden["values"]
        if "expected" in golden:
            expected = golden["expected"]

    try:
        batch = obj.compute(test_values)
        st = obj.new_state()
        inc = [obj.update(st, v) for v in test_values]

        if len(batch) != len(inc):
            problems.append(
                f"Batch and incremental output lengths differ: {len(batch)} vs {len(inc)}"
            )
        else:
            for idx, (b_val, i_val) in enumerate(zip(batch, inc, strict=True)):
                if (b_val is None) != (i_val is None):
                    problems.append(
                        f"Mismatch at index {idx}: batch={b_val} but incremental={i_val}"
                    )
                    break
                if b_val is not None and i_val is not None:
                    if abs(b_val - i_val) > 1e-4:
                        problems.append(
                            f"Numerical difference at index {idx}: batch={b_val} != inc={i_val}"
                        )
                        break

        if expected is not None:
            if len(batch) != len(expected):
                problems.append(
                    f"Computed output length {len(batch)} differs from expected {len(expected)}"
                )
            else:
                for idx, (b_val, exp_val) in enumerate(zip(batch, expected, strict=True)):
                    if (b_val is None) != (exp_val is None):
                        msg = f"Expected mismatch at index {idx}: b={b_val}, exp={exp_val}"
                        problems.append(msg)
                        break
                    if b_val is not None and exp_val is not None:
                        if abs(b_val - exp_val) > 1e-4:
                            msg = f"Output at index {idx} ({b_val}) != expected ({exp_val})"
                            problems.append(msg)
                            break
    except Exception as exc:
        problems.append(f"Indicator calculation raised an unexpected exception: {exc}")

    return problems


def strategy_problems(obj: Any) -> list[str]:
    """Verify Strategy protocol conformance."""
    problems: list[str] = []
    if not hasattr(obj, "params_schema"):
        problems.append("Strategy must define 'params_schema'")
    problems.extend(
        _check_methods(
            obj,
            ["on_start", "on_bar", "on_tick", "on_fill", "on_timer", "on_stop", "state"],
            "Strategy",
        )
    )
    return problems


def risk_rule_problems(obj: Any) -> list[str]:
    """Verify RiskRule protocol conformance."""
    return _check_methods(obj, ["check"], "RiskRule")


def notifier_problems(obj: Any) -> list[str]:
    """Verify Notifier protocol conformance. send() must never raise."""
    problems = _check_methods(obj, ["send"], "Notifier")
    if problems:
        return problems

    test_msg = Message(text="conformance test message")
    try:
        obj.send(test_msg)
    except Exception as exc:
        problems.append(f"Notifier.send() raised an exception: {exc}")
    return problems


def calendar_problems(obj: Any) -> list[str]:
    """Verify Calendar protocol conformance.

    Dates outside coverage must raise CalendarCoverageError.
    """
    problems = _check_methods(obj, ["is_trading_day", "sessions", "next_session"], "Calendar")
    if problems:
        return problems

    outside_date = date(1900, 1, 1)
    try:
        obj.is_trading_day(outside_date)
        problems.append("Calendar.is_trading_day() did not raise CalendarCoverageError")
    except CalendarCoverageError:
        pass
    except Exception as exc:
        msg = f"Calendar raised {type(exc).__name__} instead of CalendarCoverageError: {exc}"
        problems.append(msg)

    # Check sessions on coverage start date have tradable: bool
    if hasattr(obj, "coverage"):
        try:
            start_d, _ = obj.coverage()
            sess_list = obj.sessions(start_d)
            for s in sess_list:
                if not hasattr(s, "tradable") or not isinstance(s.tradable, bool):
                    problems.append(f"TradingSession {s} missing boolean 'tradable' attribute")
        except Exception:
            pass

    return problems


def clock_problems(obj: Any) -> list[str]:
    """Verify Clock protocol conformance (now, monotonic_ns)."""
    problems = _check_methods(obj, ["now", "monotonic_ns"], "Clock")
    if problems:
        return problems

    try:
        ts = obj.now()
        if not isinstance(ts, datetime) or ts.tzinfo is None:
            problems.append(f"Clock.now() returned {ts!r}, expected timezone-aware UTC datetime")
    except Exception as exc:
        problems.append(f"Clock.now() raised {type(exc).__name__}: {exc}")

    try:
        mono = obj.monotonic_ns()
        if not isinstance(mono, int):
            problems.append(f"Clock.monotonic_ns() returned {type(mono).__name__}, expected int")
    except Exception as exc:
        problems.append(f"Clock.monotonic_ns() raised {type(exc).__name__}: {exc}")

    return problems


def secrets_problems(obj: Any) -> list[str]:
    """Verify SecretsProvider protocol conformance.

    Missing keys must raise ConfigError; names() returns keys only.
    """
    problems = _check_methods(obj, ["get", "set", "names"], "SecretsProvider")
    if problems:
        return problems

    try:
        names = obj.names()
        if not isinstance(names, (list, tuple, set, Sequence)):
            problems.append(
                f"SecretsProvider.names() returned {type(names).__name__}, expected Sequence"
            )
    except Exception as exc:
        problems.append(f"SecretsProvider.names() raised an exception: {exc}")

    missing_key = "__algodesk_nonexistent_secret_key_conformance_check__"
    try:
        obj.get(missing_key)
        problems.append("SecretsProvider.get() did not raise ConfigError for missing key")
    except ConfigError:
        pass
    except Exception as exc:
        problems.append(
            f"SecretsProvider.get() raised {type(exc).__name__} instead of ConfigError: {exc}"
        )

    return problems


def fees_problems(obj: Any) -> list[str]:
    """Verify that a fees plugin exposes a callable schedule() returning schedule data."""
    problems: list[str] = []
    if not hasattr(obj, "schedule") or not callable(obj.schedule):
        problems.append("Fees plugin must define callable schedule()")
        return problems

    try:
        sch = obj.schedule()
        has_comp = hasattr(sch, "components") or (
            isinstance(sch, Mapping) and ("component" in sch or "components" in sch)
        )
        has_meta = hasattr(sch, "meta") or (isinstance(sch, Mapping) and "meta" in sch)
        if not (has_comp and has_meta):
            problems.append("schedule() must return an object with 'components' and 'meta'")
    except Exception as exc:
        problems.append(f"schedule() raised an exception: {exc}")

    return problems


def kind_problems(plugin: Any) -> list[str]:
    """Dispatch plugin conformance verification based on manifest.kind."""
    if not hasattr(plugin, "manifest"):
        return ["Plugin has no 'manifest' attribute"]

    kind_raw = getattr(plugin.manifest, "kind", None)
    if kind_raw is None:
        return ["Plugin manifest has no 'kind' attribute"]

    kind_str = (kind_raw.value if hasattr(kind_raw, "value") else str(kind_raw)).lower()

    if kind_str == "broker":
        return data_source_problems(plugin)
    if kind_str == "venue":
        return venue_problems(plugin)
    if kind_str == "indicator":
        return indicator_problems(plugin)
    if kind_str == "strategy":
        return strategy_problems(plugin)
    if kind_str == "risk_rule":
        return risk_rule_problems(plugin)
    if kind_str == "notifier":
        return notifier_problems(plugin)
    if kind_str == "calendar":
        return calendar_problems(plugin)
    if kind_str == "secrets":
        return secrets_problems(plugin)
    if kind_str in ("fees", "fee"):
        return fees_problems(plugin)
    if kind_str == "clock":
        return clock_problems(plugin)

    return []
