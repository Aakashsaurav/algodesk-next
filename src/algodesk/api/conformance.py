"""Pure conformance verification helpers per plugin kind.

No test runner or pytest imports. Returns a list of problem strings.
"""

from __future__ import annotations

import ast
import inspect
import math
import tomllib
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
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
        [
            "configure",
            "place",
            "cancel",
            "order_status",
            "end_of_day",
            "on_quote",
            "events",
            "positions",
            "funds",
        ],
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


def _is_none_or_nan(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, float) and math.isnan(val):
        return True
    if isinstance(val, str) and val.lower() in ("none", "null", "nan"):
        return True
    return False


def _check_series_equality(
    computed: Sequence[float | None],
    reference: Sequence[float | None],
    name: str,
    kind: str,
) -> list[str]:
    problems: list[str] = []
    if len(computed) != len(reference):
        return [
            f"Output '{name}': {kind} length {len(computed)} differs from expected {len(reference)}"
        ]
    for idx, (c_val, r_val) in enumerate(zip(computed, reference, strict=True)):
        c_none = _is_none_or_nan(c_val)
        r_none = _is_none_or_nan(r_val)
        if c_none != r_none:
            problems.append(f"Output '{name}' mismatch at index {idx}: comp={c_val}, ref={r_val}")
            break
        if not c_none and not r_none:
            if abs(float(c_val) - float(r_val)) > 1e-4:  # type: ignore[arg-type]
                problems.append(f"Numerical difference at index {idx}: comp={c_val} != ref={r_val}")
                break
    return problems


def _check_indicator_batch_incremental(
    batch: Sequence[float | None] | Mapping[str, Sequence[float | None]],
    inc: list[Any],
) -> list[str]:
    problems: list[str] = []
    if isinstance(batch, Mapping):
        for out_name, b_series in batch.items():
            i_series = [r.get(out_name) if isinstance(r, Mapping) else r for r in inc]
            problems.extend(_check_series_equality(b_series, i_series, out_name, "batch vs inc"))
    else:
        problems.extend(_check_series_equality(batch, inc, "output", "batch vs inc"))
    return problems


def _check_indicator_expected(
    batch: Sequence[float | None] | Mapping[str, Sequence[float | None]],
    expected: Any,
) -> list[str]:
    if expected is None:
        return []
    problems: list[str] = []
    if isinstance(expected, Mapping):
        for out_name, exp_series in expected.items():
            b_series = (
                batch[out_name] if isinstance(batch, Mapping) and out_name in batch else batch
            )
            problems.extend(_check_series_equality(b_series, exp_series, out_name, "expected"))  # type: ignore[arg-type]
    elif isinstance(batch, Mapping):
        first_key = next(iter(batch.keys()))
        problems.extend(_check_series_equality(batch[first_key], expected, first_key, "expected"))
    else:
        problems.extend(_check_series_equality(batch, expected, "output", "expected"))
    return problems


def _check_indicator_warmup_and_lookahead(
    ind: Any,
    inputs: Any,
    rows: list[Any],
    batch: Sequence[float | None] | Mapping[str, Sequence[float | None]],
) -> list[str]:
    problems: list[str] = []
    warmup = int(getattr(getattr(ind, "spec", None), "warmup", 0) or 0)
    if isinstance(batch, Mapping):
        min_len = min((len(s) for s in batch.values()), default=0)
        for idx in range(min(warmup, min_len)):
            if all(not _is_none_or_nan(b_series[idx]) for b_series in batch.values()):
                problems.append(
                    f"All outputs at warmup index {idx} are non-None, expected warmup {warmup}"
                )
                break
    else:
        for idx in range(min(warmup, len(batch))):
            if not _is_none_or_nan(batch[idx]):
                problems.append(f"Output at warmup index {idx} is {batch[idx]}, expected None")
                break

    if len(rows) > warmup + 1:
        prefix_len = max(warmup + 1, len(rows) - 2)
        sub_inputs = (
            {k: inputs[k][:prefix_len] for k in inputs}
            if isinstance(inputs, Mapping)
            else inputs[:prefix_len]
        )
        try:
            sub_batch = ind.compute(sub_inputs)
            if isinstance(sub_batch, Mapping):
                for k, sub_s in sub_batch.items():
                    orig_s = batch[k][:prefix_len]  # type: ignore[index]
                    problems.extend(
                        _check_series_equality(sub_s, orig_s, k, "look-ahead prefix vs full")
                    )
            else:
                orig_s = batch[:prefix_len]  # type: ignore[index]
                problems.extend(
                    _check_series_equality(sub_batch, orig_s, "output", "look-ahead prefix vs full")
                )
        except Exception as exc:
            problems.append(f"Look-ahead check raised an exception: {exc}")
    return problems


def _verify_indicator_vector(
    ind: Any,
    inputs: Any,
    expected: Any | None,
) -> list[str]:
    try:
        batch = ind.compute(inputs)
    except Exception as exc:
        return [f"Indicator.compute() raised an exception: {exc}"]

    if isinstance(inputs, Mapping):
        keys = list(inputs.keys())
        n_rows = len(inputs[keys[0]]) if keys else 0
        rows = [{k: inputs[k][i] for k in keys} for i in range(n_rows)]
    else:
        rows = list(inputs)

    try:
        st = ind.new_state()
        inc = [ind.update(st, r) for r in rows]
    except Exception as exc:
        return [f"Indicator.update() raised an exception: {exc}"]

    problems = _check_indicator_batch_incremental(batch, inc)
    if expected is not None:
        problems.extend(_check_indicator_expected(batch, expected))
    problems.extend(_check_indicator_warmup_and_lookahead(ind, inputs, rows, batch))
    return problems


def _load_golden_vectors(
    obj: Any, golden: Mapping[str, Any] | None
) -> tuple[list[dict[str, Any]], str | None]:
    no_vec_err = (
        "golden.toml has no vectors: specify at least one golden vector "
        "with inputs and expected outputs in golden.toml"
    )
    if golden is not None:
        if not golden:
            return [], no_vec_err
        if "vectors" in golden:
            v_val = golden["vectors"]
            v_list = list(v_val.values()) if isinstance(v_val, Mapping) else list(v_val)
            return v_list, None
        if "inputs" in golden or "values" in golden:
            return [dict(golden)], None
        return [], no_vec_err

    p_dir = getattr(obj, "directory", None) or getattr(
        getattr(obj, "manifest", None), "directory", None
    )
    if p_dir is None:
        try:
            p_dir = Path(inspect.getfile(obj.__class__)).parent
        except Exception:
            p_dir = None

    if p_dir is not None:
        g_file = Path(p_dir) / "golden.toml"
        if g_file.exists():
            text = g_file.read_text(encoding="utf-8")
            if not text.strip():
                return [], no_vec_err
            try:
                parsed = tomllib.loads(text)
            except Exception as exc:
                return [], f"Failed to parse golden.toml: {exc}"
            if "vectors" in parsed:
                v_val = parsed["vectors"]
                v_list = list(v_val.values()) if isinstance(v_val, Mapping) else list(v_val)
                if not v_list:
                    return [], no_vec_err
                return v_list, None
            if "inputs" in parsed or "values" in parsed:
                return [parsed], None
            return [], no_vec_err
        if (
            hasattr(obj, "manifest")
            and str(getattr(obj.manifest, "kind", "")).lower() == "indicator"
        ):
            return [], f"golden.toml not found in {p_dir}: specify at least one golden vector"

    return [{"inputs": [10.0, 11.5, 12.0, 11.0, 13.0, 14.5, 13.5, 15.0]}], None


def indicator_problems(obj: Any, golden: Mapping[str, Any] | None = None) -> list[str]:
    """Verify Indicator conformance: golden vectors, batch/incremental, warmup, lookahead."""
    problems: list[str] = []
    if not hasattr(obj, "spec") and not hasattr(obj, "get_indicator"):
        problems.append("Indicator must define a 'spec' attribute of type IndicatorSpec")
        return problems
    if hasattr(obj, "spec"):
        problems.extend(_check_methods(obj, ["compute", "new_state", "update"], "Indicator"))
        if problems:
            return problems

    vectors, err = _load_golden_vectors(obj, golden)
    if err is not None:
        return [err]

    for vec in vectors:
        if isinstance(vec, Mapping) and "indicator" in vec and hasattr(obj, "get_indicator"):
            try:
                ind = obj.get_indicator(vec["indicator"], **vec.get("params", {}))
            except Exception as exc:
                problems.append(f"Failed to create indicator '{vec['indicator']}': {exc}")
                continue
        elif hasattr(obj, "get_indicator"):
            ind_name = getattr(getattr(obj, "spec", None), "name", "sma")
            ind = obj.get_indicator(ind_name, **vec.get("params", {}))
        else:
            ind = obj

        inputs = vec.get("inputs", vec.get("values"))
        expected = vec.get("expected")
        if inputs is None:
            continue
        problems.extend(_verify_indicator_vector(ind, inputs, expected))

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


def reference_problems(obj: Any) -> list[str]:
    """Verify ReferenceSource protocol conformance and manifest declarations."""
    problems = _check_methods(obj, ["corporate_actions"], "ReferenceSource")
    ca_fn = getattr(obj, "corporate_actions", None)
    if ca_fn is not None and callable(ca_fn):
        try:
            sig = inspect.signature(ca_fn)
            params = list(sig.parameters.values())
            if not params:
                problems.append("ReferenceSource.corporate_actions() must accept symbol_or_isin")
        except Exception:
            pass

    manifest = getattr(obj, "manifest", None)
    if manifest is not None:
        limits = getattr(manifest, "limits", None)
        if not limits or not isinstance(limits, Mapping):
            problems.append("Reference plugin manifest must declare [limits] per ADR-042")
        elif "corporate_actions" not in limits:
            problems.append(
                "Reference plugin manifest [limits] must define 'corporate_actions' endpoint"
            )

        verified_on = getattr(manifest, "verified_on", None)
        if verified_on is None:
            problems.append("Reference plugin manifest must declare 'verified_on' date")

        sources = getattr(manifest, "sources", None)
        if not sources or not isinstance(sources, Sequence):
            problems.append("Reference plugin manifest must declare non-empty 'sources' list")

    return problems


class _SyntheticSymbolFeatureView:
    def __init__(self, symbol: str, price: float) -> None:
        self.symbol = symbol
        self._price = price

    @property
    def close(self) -> float | None:
        return self._price

    @property
    def open(self) -> float | None:
        return self._price

    @property
    def high(self) -> float | None:
        return self._price * 1.01

    @property
    def low(self) -> float | None:
        return self._price * 0.99

    @property
    def volume(self) -> float | None:
        return 1000.0

    def get_bar(self, field: str, bars_ago: int = 0) -> float | None:
        if field == "close":
            return self._price
        if field == "open":
            return self._price
        if field == "high":
            return self._price * 1.01
        if field == "low":
            return self._price * 0.99
        if field == "volume":
            return 1000.0
        return None

    def get_indicator(
        self,
        name: str,
        output: str | None = None,
        bars_ago: int = 0,
        **params: Any,
    ) -> float | None:
        return self._price


class _SyntheticFeatureView:
    def __init__(self) -> None:
        self.symbols = ["AAA", "BBB"]
        self._views = {
            "AAA": _SyntheticSymbolFeatureView("AAA", 100.0),
            "BBB": _SyntheticSymbolFeatureView("BBB", 200.0),
        }

    def for_symbol(self, symbol: str) -> _SyntheticSymbolFeatureView:
        return self._views.get(symbol, _SyntheticSymbolFeatureView(symbol, 100.0))

    def get_bar(self, symbol: str, field: str, bars_ago: int = 0) -> float | None:
        return self.for_symbol(symbol).get_bar(field, bars_ago)

    def get_indicator(
        self,
        symbol: str,
        name: str,
        output: str | None = None,
        bars_ago: int = 0,
        **params: Any,
    ) -> float | None:
        return self.for_symbol(symbol).get_indicator(name, output, bars_ago, **params)

    def universe_table(self) -> Mapping[str, Mapping[str, float | None]]:
        return {s: {"close": self.for_symbol(s).close} for s in self.symbols}


def screen_problems(obj: Any) -> list[str]:
    """Verify Screen protocol conformance."""
    problems: list[str] = []
    if not hasattr(obj, "spec"):
        problems.append("Screen must define 'spec' attribute")
    else:
        spec = obj.spec
        for attr in ("name", "description", "timeframe", "output_columns"):
            if not hasattr(spec, attr):
                problems.append(f"Screen.spec missing '{attr}'")

    if not hasattr(obj, "evaluate") or not callable(obj.evaluate):
        problems.append("Screen must define callable 'evaluate(features)'")
        return problems

    # Check for forbidden imports in module source (read-only, no data/broker/venue imports)
    file_path: str | None = None
    try:
        file_path = inspect.getfile(type(obj))
    except Exception:
        mod = inspect.getmodule(type(obj))
        file_path = getattr(mod, "__file__", None) if mod else None

    if file_path and Path(file_path).is_file():
        try:
            with open(file_path, encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for forbidden in (
                            "algodesk.data",
                            "algodesk.oms",
                            "broker",
                            "venue",
                        ):
                            if forbidden in alias.name:
                                problems.append(
                                    f"Screen plugin imports forbidden module '{alias.name}'"
                                )
                elif isinstance(node, ast.ImportFrom):
                    mod_name = node.module or ""
                    for forbidden in (
                        "algodesk.data",
                        "algodesk.oms",
                        "broker",
                        "venue",
                    ):
                        if forbidden in mod_name:
                            problems.append(
                                f"Screen plugin imports from forbidden module '{mod_name}'"
                            )
        except Exception:
            pass

    # Run synthetic evaluation
    try:
        synth = _SyntheticFeatureView()
        rows1 = obj.evaluate(synth)
        if not isinstance(rows1, Sequence) or isinstance(rows1, (str, bytes)):
            problems.append(f"Screen.evaluate() returned {type(rows1).__name__}, expected Sequence")
            return problems

        for i, row in enumerate(rows1):
            if not hasattr(row, "symbol") or not hasattr(row, "passed"):
                problems.append(f"ScreenRow at index {i} missing 'symbol' or 'passed'")
            if not hasattr(row, "score") or not isinstance(row.score, (int, float)):
                problems.append(f"ScreenRow at index {i} missing numeric 'score'")
            if not hasattr(row, "values") or not isinstance(row.values, Mapping):
                problems.append(f"ScreenRow at index {i} missing Mapping 'values'")

        rows2 = obj.evaluate(synth)
        if len(rows1) != len(rows2):
            problems.append("Screen.evaluate() is not deterministic: row count changed")
        else:
            for r1, r2 in zip(rows1, rows2, strict=True):
                if (
                    getattr(r1, "symbol", None) != getattr(r2, "symbol", None)
                    or getattr(r1, "passed", None) != getattr(r2, "passed", None)
                    or getattr(r1, "score", None) != getattr(r2, "score", None)
                ):
                    problems.append("Screen.evaluate() is not deterministic: output values changed")
                    break
    except Exception as exc:
        problems.append(f"Screen.evaluate() raised exception during test: {exc}")

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
    if kind_str == "screen":
        return screen_problems(plugin)
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
    if kind_str == "reference":
        return reference_problems(plugin)

    return []


def bar_sequence_conformance_problems(
    bars: Sequence[Any],
    start: datetime,
    end: datetime,
    timeframe: str = "1d",
) -> list[str]:
    """Verify that a sequence of bars is timezone-aware UTC, strictly monotonic,
    and within range.
    """
    problems: list[str] = []
    prev_ts: datetime | None = None
    for i, b in enumerate(bars):
        st = getattr(b, "start_ts", None)
        et = getattr(b, "end_ts", None)
        if st is None or getattr(st, "tzinfo", None) is None:
            problems.append(f"Bar {i} start_ts is missing or not timezone-aware")
            continue
        if et is None or getattr(et, "tzinfo", None) is None:
            problems.append(f"Bar {i} end_ts is missing or not timezone-aware")
            continue
        if st.utcoffset() != timedelta(0):
            problems.append(f"Bar {i} start_ts is not UTC")
        if et.utcoffset() != timedelta(0):
            problems.append(f"Bar {i} end_ts is not UTC")
        if prev_ts is not None and st <= prev_ts:
            problems.append(
                f"Bar {i} timestamp {st.isoformat()} is not strictly monotonic "
                f"after {prev_ts.isoformat()}"
            )
        prev_ts = st
        if timeframe == "1d":
            from zoneinfo import ZoneInfo

            ist = ZoneInfo("Asia/Kolkata")
            bd = st.astimezone(ist).date()
            sd = start.astimezone(ist).date() if start.tzinfo else start.date()
            ed = end.astimezone(ist).date() if end.tzinfo else end.date()
            if bd < sd or bd > ed:
                problems.append(
                    f"Bar {i} date {bd.isoformat()} is outside range "
                    f"[{sd.isoformat()}, {ed.isoformat()}]"
                )
        else:
            if st < start or st > end:
                problems.append(
                    f"Bar {i} timestamp {st.isoformat()} is outside range "
                    f"[{start.isoformat()}, {end.isoformat()}]"
                )
    return problems
