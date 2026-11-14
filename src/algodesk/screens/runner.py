"""Screen execution runner orchestrating data fetching, rules, ranking, and hashes."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from algodesk.api.errors import CalendarCoverageError
from algodesk.api.market import Bar
from algodesk.api.protocols import Calendar, Clock
from algodesk.api.screens import (
    IndicatorResolver,
    Screen,
    ScreenBars,
    ScreenDataReader,
    ScreenRefusal,
    ScreenResult,
    ScreenRow,
    SkipReason,
)
from algodesk.screens.features import build_feature_view
from algodesk.screens.models import OperandModel, TomlScreenModel
from algodesk.screens.ranking import apply_ranking
from algodesk.screens.rules import RuleEvaluationError, _eval_operand, eval_screen_rules

logger = logging.getLogger("algodesk.screens.runner")
IST = ZoneInfo("Asia/Kolkata")


def compute_result_hash(
    universe_hash: str,
    provenance_by_symbol: Mapping[str, Mapping[str, Any]],
    skipped: Sequence[tuple[str, Any, str]],
    screen_file_hash: str,
    as_of: date,
    parameters: Mapping[str, Any],
    flags: Mapping[str, Any],
) -> str:
    """Compute deterministic SHA-256 result hash across data provenance and screen run inputs."""
    prov_entries: list[dict[str, Any]] = []
    all_syms = set(provenance_by_symbol.keys()) | {s[0] for s in skipped}
    for sym in sorted(all_syms):
        prov = provenance_by_symbol.get(sym)
        if prov is not None:
            csums = sorted(prov.get("checksums", ()))
            act_hash = str(prov.get("actions_hash", "") or "")
            ds_ids = sorted(prov.get("dataset_ids", ()))
            prov_entries.append(
                {
                    "symbol": sym,
                    "dataset_ids": ds_ids,
                    "checksums": csums,
                    "actions_hash": act_hash,
                }
            )
        else:
            prov_entries.append({"symbol": sym, "skipped": True})

    payload = {
        "as_of": str(as_of),
        "flags": dict(sorted(flags.items())),
        "parameters": dict(sorted(parameters.items())),
        "provenance": prov_entries,
        "screen_file_hash": screen_file_hash,
        "universe_hash": universe_hash,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _format_skip_detail(sym: str, reason: SkipReason, detail: str) -> str:
    r_val = reason.value if hasattr(reason, "value") else str(reason)
    clean_sym = sym.split(":")[-1]
    if "Fix with:" in detail:
        return detail
    if reason == SkipReason.ADJUSTMENT_REFUSED:
        fix = f"Fix with: algodesk data actions override add --symbol {clean_sym} ..."
    else:
        fix = f"Fix with: algodesk data accumulate --symbols {clean_sym} --days 500"
    return f"{clean_sym}: {r_val}: {detail}. {fix}"


class ScreenRunner:
    """Runner executing screen logic across an instrument universe."""

    def __init__(
        self,
        data_reader: ScreenDataReader,
        indicator_resolver: IndicatorResolver,
        calendar: Calendar | None = None,
        max_skipped_pct: float | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.data_reader = data_reader
        self.indicator_resolver = indicator_resolver
        self.calendar = calendar
        self.max_skipped_pct = float(max_skipped_pct) if max_skipped_pct is not None else float(10)
        self.clock = clock

    def resolve_as_of(self, as_of: date | str | None) -> date:
        """Resolve effective as-of date (last completed session on or before as-of)."""
        if as_of is not None:
            target_date = (
                datetime.strptime(as_of, "%Y-%m-%d").date() if isinstance(as_of, str) else as_of
            )
        else:
            now_dt = self.clock.now() if self.clock is not None else datetime.now(UTC)
            ist_now = now_dt.astimezone(IST)
            # Market settle margin: 15:45 IST
            settle_time = time(15, 45)
            if ist_now.time() >= settle_time:
                target_date = ist_now.date()
            else:
                target_date = ist_now.date() - timedelta(days=1)

        if self.calendar is None:
            return target_date

        # Check calendar coverage first; dates outside coverage must fail loudly
        if hasattr(self.calendar, "coverage"):
            start_cov, end_cov = self.calendar.coverage()
            if target_date < start_cov or target_date > end_cov:
                raise CalendarCoverageError(
                    what=(
                        f"Requested date {target_date} outside calendar coverage "
                        f"[{start_cov}, {end_cov}]"
                    ),
                    where="ScreenRunner.resolve_as_of",
                    fix=f"Request a date between {start_cov} and {end_cov}",
                )

        # Walk backwards to find last completed trading session on or before target_date
        cur = target_date
        for _ in range(30):
            try:
                if self.calendar.is_trading_day(cur):
                    return cur
            except CalendarCoverageError:
                raise
            except Exception:
                pass
            cur -= timedelta(days=1)

        return target_date

    def run(
        self,
        screen: TomlScreenModel | Screen,
        symbols: Sequence[str],
        as_of: date | str | None = None,
        limit: int | None = None,
        adjust: str | None = None,
        accept_unverified: bool = False,
        parameters: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        screen_file_hash: str | None = None,
    ) -> ScreenResult:
        """Execute screen over universe and return ScreenResult."""
        as_of_date = self.resolve_as_of(as_of)

        screen_name = (
            screen.name
            if isinstance(screen, TomlScreenModel)
            else getattr(getattr(screen, "spec", None), "name", "screen")
        )
        timeframe = (
            screen.timeframe
            if isinstance(screen, TomlScreenModel)
            else getattr(getattr(screen, "spec", None), "timeframe", "1d")
        )
        adjust_mode: str = str(
            adjust
            or getattr(screen, "adjust", None)
            or getattr(getattr(screen, "spec", None), "adjust", None)
            or "splits"
        )

        sym_list = list(symbols)
        if limit is not None and limit > 0:
            sym_list = sym_list[:limit]

        universe_hash = hashlib.sha256(",".join(sorted(sym_list)).encode("utf-8")).hexdigest()

        # Date boundaries for data fetch (reads end at as-of session close in UTC: no look-ahead)
        end_dt = datetime.combine(as_of_date, time(23, 59, 59), tzinfo=IST).astimezone(UTC)
        start_dt = datetime.combine(
            as_of_date - timedelta(days=730), time(0, 0), tzinfo=IST
        ).astimezone(UTC)

        bars_by_symbol: dict[str, tuple[Bar, ...]] = {}
        provenance_by_symbol: dict[str, Mapping[str, Any]] = {}
        skipped: list[tuple[str, SkipReason, str]] = []

        t0_reads = perf_counter()
        for sym in sym_list:
            try:
                res = self.data_reader.get_bars(
                    instrument=sym,
                    timeframe=timeframe,
                    start=start_dt,
                    end=end_dt,
                    adjust=adjust_mode,
                    accept_unverified=accept_unverified,
                )
            except Exception as exc:
                detail = _format_skip_detail(sym, SkipReason.NO_DATA, f"Data fetch error: {exc}")
                skipped.append((sym, SkipReason.NO_DATA, detail))
                continue

            if isinstance(res, ScreenRefusal):
                detail = _format_skip_detail(sym, res.reason, res.detail)
                skipped.append((sym, res.reason, detail))
            elif isinstance(res, ScreenBars):
                if not res.bars:
                    detail = _format_skip_detail(
                        sym, SkipReason.NO_DATA, f"No bars found up to {as_of_date}"
                    )
                    skipped.append((sym, SkipReason.NO_DATA, detail))
                else:
                    # Filter bars strictly <= end_dt
                    bars_up_to_as_of = tuple(b for b in res.bars if b.end_ts <= end_dt)
                    if not bars_up_to_as_of:
                        detail = _format_skip_detail(
                            sym,
                            SkipReason.NO_DATA,
                            f"No completed bars on or before {as_of_date}",
                        )
                        skipped.append((sym, SkipReason.NO_DATA, detail))
                    else:
                        bars_by_symbol[sym] = bars_up_to_as_of
                        provenance_by_symbol[sym] = res.provenance
            elif isinstance(res, tuple) and len(res) == 2:
                # Tuple variant: (bars, prov) or (None, (reason, detail))
                bars_val, prov_or_refusal = res
                if bars_val is None:
                    if isinstance(prov_or_refusal, tuple) and len(prov_or_refusal) == 2:
                        r_code, r_msg = prov_or_refusal
                        detail = _format_skip_detail(sym, r_code, r_msg)
                        skipped.append((sym, r_code, detail))
                    else:
                        detail = _format_skip_detail(sym, SkipReason.NO_DATA, str(prov_or_refusal))
                        skipped.append((sym, SkipReason.NO_DATA, detail))
                else:
                    bars_up_to = tuple(b for b in bars_val if b.end_ts <= end_dt)
                    if not bars_up_to:
                        detail = _format_skip_detail(
                            sym,
                            SkipReason.NO_DATA,
                            f"No completed bars on or before {as_of_date}",
                        )
                        skipped.append((sym, SkipReason.NO_DATA, detail))
                    else:
                        bars_by_symbol[sym] = bars_up_to
                        provenance_by_symbol[sym] = (
                            prov_or_refusal if isinstance(prov_or_refusal, Mapping) else {}
                        )
        t_reads = perf_counter() - t0_reads

        # Extract required indicators
        required_inds: list[dict[str, Any]] = []
        if isinstance(screen, TomlScreenModel):
            for req in screen.require:
                for op in (req.left, req.right):
                    if isinstance(op, OperandModel) and op.is_indicator:
                        assert op.ind is not None
                        required_inds.append({"ind": op.ind, "params": op.params})
            for grp in screen.group:
                for req in grp.require:
                    for op in (req.left, req.right):
                        if isinstance(op, OperandModel) and op.is_indicator:
                            assert op.ind is not None
                            required_inds.append({"ind": op.ind, "params": op.params})
            if screen.rank and screen.rank.by.is_indicator:
                assert screen.rank.by.ind is not None
                required_inds.append({"ind": screen.rank.by.ind, "params": screen.rank.by.params})
        else:
            spec = getattr(screen, "spec", None)
            if spec and hasattr(spec, "required_indicators"):
                required_inds.extend(spec.required_indicators)

        valid_symbols = [s for s in sym_list if s in bars_by_symbol]
        t0_ind = perf_counter()
        feature_view = build_feature_view(
            symbols=valid_symbols,
            bars_by_symbol=bars_by_symbol,
            required_indicators=required_inds,
            indicator_resolver=self.indicator_resolver,
        )
        t_indicators = perf_counter() - t0_ind

        t0_rules = perf_counter()
        rows: list[ScreenRow] = []
        if isinstance(screen, TomlScreenModel):
            for sym in valid_symbols:
                sym_view = feature_view.for_symbol(sym)
                try:
                    row = eval_screen_rules(screen, sym_view)
                    rows.append(row)
                except RuleEvaluationError as exc:
                    detail = _format_skip_detail(sym, exc.reason, exc.message)
                    skipped.append((sym, exc.reason, detail))
                except Exception as exc:
                    detail = _format_skip_detail(sym, SkipReason.INDICATOR_ERROR, str(exc))
                    skipped.append((sym, SkipReason.INDICATOR_ERROR, detail))

            # Apply ranking if specified
            if screen.rank is not None:
                metric_values: dict[str, float] = {}
                evaluated_syms = [r.symbol for r in rows]
                for s in list(evaluated_syms):
                    sym_view = feature_view.for_symbol(s)
                    try:
                        m_val = _eval_operand(screen.rank.by, sym_view, 0)
                        if m_val is None:
                            detail = _format_skip_detail(
                                s, SkipReason.INSUFFICIENT_HISTORY, "Rank metric returned None"
                            )
                            skipped.append((s, SkipReason.INSUFFICIENT_HISTORY, detail))
                            rows = [r for r in rows if r.symbol != s]
                        else:
                            metric_values[s] = float(m_val)
                    except Exception as exc:
                        detail = _format_skip_detail(
                            s, SkipReason.INDICATOR_ERROR, f"Rank error: {exc}"
                        )
                        skipped.append((s, SkipReason.INDICATOR_ERROR, detail))
                        rows = [r for r in rows if r.symbol != s]

                rows = apply_ranking(rows, screen.rank, metric_values)
        else:
            try:
                rows.extend(screen.evaluate(feature_view))
            except Exception as exc:
                logger.error("Screen plugin evaluate() failed: %s", exc)
        t_rules = perf_counter() - t0_rules

        # File hash resolution
        resolved_screen_hash = screen_file_hash
        if resolved_screen_hash is None:
            if hasattr(screen, "screen_file_hash") and screen.screen_file_hash:
                resolved_screen_hash = str(screen.screen_file_hash)
            elif (
                hasattr(screen, "spec")
                and hasattr(screen.spec, "screen_file_hash")
                and screen.spec.screen_file_hash
            ):
                resolved_screen_hash = str(screen.spec.screen_file_hash)
            else:
                spec_repr = f"{screen_name}:{timeframe}:{adjust_mode}"
                resolved_screen_hash = hashlib.sha256(spec_repr.encode("utf-8")).hexdigest()

        flags_dict = {
            "adjust": adjust_mode,
            "accept_unverified": accept_unverified,
        }
        res_hash = compute_result_hash(
            universe_hash=universe_hash,
            provenance_by_symbol=provenance_by_symbol,
            skipped=skipped,
            screen_file_hash=resolved_screen_hash,
            as_of=as_of_date,
            parameters=parameters or {},
            flags=flags_dict,
        )

        # Check skip guard
        total_syms = len(sym_list)
        skipped_count = len(skipped)
        skipped_pct = (skipped_count / total_syms * 100.0) if total_syms > 0 else 0.0

        verdict = "PASS"
        fail_reason: str | None = None
        if skipped_pct > self.max_skipped_pct:
            verdict = "FAIL"
            fail_reason = (
                f"Skipped percentage {skipped_pct:.1f}% exceeds maximum allowed "
                f"screen.max_skipped_pct ({self.max_skipped_pct:.1f}%)"
            )

        now_str = (self.clock.now() if self.clock is not None else datetime.now(UTC)).strftime(
            "%Y%m%d_%H%M%S"
        )
        r_id = run_id or f"screen_{screen_name}_{as_of_date.strftime('%Y%m%d')}_{now_str}"

        return ScreenResult(
            name=screen_name,
            as_of=as_of_date,
            timeframe=timeframe,
            rows=rows,
            skipped=skipped,
            universe_hash=universe_hash,
            result_hash=res_hash,
            provenance=provenance_by_symbol,
            parameters=dict(parameters or {}),
            flags=flags_dict,
            verdict=verdict,
            fail_reason=fail_reason,
            run_id=r_id,
            timing={
                "reads": t_reads,
                "indicators": t_indicators,
                "rules": t_rules,
            },
        )
