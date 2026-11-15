"""Screen execution runner orchestrating data fetching, rules, ranking, and hashes."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
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


def _format_skip_detail(sym: str, reason: SkipReason, detail: str, group: str | None = None) -> str:
    r_val = reason.value if hasattr(reason, "value") else str(reason)
    clean_sym = sym.split(":")[-1]
    if "Fix with:" in detail:
        if group and group != "G" and "--group G" in detail:
            detail = detail.replace("--group G", f"--group {group}")
        if f"{clean_sym}: {r_val}:" in detail:
            return detail
        return f"{clean_sym}: {r_val}: {detail}"
    if "coverage and bars disagree" in detail or "coverage repair" in detail:
        if f"{clean_sym}: {r_val}:" in detail:
            return detail
        return f"{clean_sym}: {r_val}: {detail}"
    if reason == SkipReason.ADJUSTMENT_REFUSED:
        fix = f"Fix with: algodesk data actions override add --symbol {clean_sym} ..."
    else:
        grp_str = f"--group {group}" if group else "--group G"
        fix = f"Fix with: algodesk data accumulate {grp_str} --timeframe 1d"
    return f"{clean_sym}: {r_val}: {detail}. {fix}"


def _extract_required_indicators(screen: Any) -> list[dict[str, Any]]:
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
    return required_inds


def _compute_max_warmup(
    required_inds: Sequence[Mapping[str, Any]],
    resolver: IndicatorResolver | None = None,
) -> int:
    max_warmup = 0
    for ind_info in required_inds:
        p_dict = ind_info.get("params", {}) or {}
        if callable(p_dict):
            p_dict = p_dict()
        if isinstance(p_dict, dict) and "params" in p_dict and isinstance(p_dict["params"], dict):
            p_dict = {**p_dict, **p_dict["params"]}
        ind_name = ind_info.get("ind")
        if resolver is not None and ind_name:
            try:
                ind = resolver.get_indicator(ind_name, **p_dict)
                if hasattr(ind, "warmup"):
                    max_warmup = max(max_warmup, int(ind.warmup))
                elif hasattr(ind, "spec") and hasattr(ind.spec, "warmup"):
                    max_warmup = max(max_warmup, int(ind.spec.warmup))
            except Exception:
                pass
        for p_key in ("period", "timeperiod", "length", "slow", "signal", "window"):
            if p_key in p_dict and isinstance(p_dict[p_key], (int, float)):
                max_warmup = max(max_warmup, int(p_dict[p_key]))
    return max_warmup


class ScreenRunner:
    """Runner executing screen logic across an instrument universe."""

    def __init__(
        self,
        data_reader: ScreenDataReader,
        indicator_resolver: IndicatorResolver,
        calendar: Calendar | None = None,
        max_skipped_pct: float | None = None,
        clock: Clock | None = None,
        progress_every: int | None = None,
        params: Any = None,
        manifest: Any = None,
        session_resolver: Callable[[date, int], list[date]] | None = None,
    ) -> None:
        self.data_reader = data_reader
        self.indicator_resolver = indicator_resolver
        self.calendar = calendar
        self.max_skipped_pct = float(max_skipped_pct) if max_skipped_pct is not None else float(10)
        self.clock = clock
        self.progress_every = int(progress_every) if progress_every is not None else 25
        self.params = params
        self.manifest = manifest
        self.session_resolver = session_resolver

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
        group: str | None = None,
    ) -> ScreenResult:
        """Execute screen over universe and return ScreenResult."""
        try:
            return self._run_internal(
                screen=screen,
                symbols=symbols,
                as_of=as_of,
                limit=limit,
                adjust=adjust,
                accept_unverified=accept_unverified,
                parameters=parameters,
                run_id=run_id,
                screen_file_hash=screen_file_hash,
                group=group,
            )
        except KeyboardInterrupt:
            return self.create_aborted_result(
                screen=screen,
                symbols=symbols,
                as_of=as_of,
                limit=limit,
                adjust=adjust,
                accept_unverified=accept_unverified,
                parameters=parameters,
                run_id=run_id,
                screen_file_hash=screen_file_hash,
                verdict="STOPPED",
                fail_reason="Screen execution interrupted by user (KeyboardInterrupt)",
            )

    def create_aborted_result(
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
        verdict: str = "FAIL",
        fail_reason: str = "Screen aborted",
    ) -> ScreenResult:
        as_of_date = self.resolve_as_of(as_of)
        s_spec = getattr(screen, "spec", None)
        if isinstance(screen, TomlScreenModel):
            s_name = screen.name
            tf = screen.timeframe
        else:
            s_name = getattr(s_spec, "name", "screen")
            tf = getattr(s_spec, "timeframe", "1d")
        adj = str(adjust or getattr(screen, "adjust", None) or getattr(s_spec, "adjust", "splits"))
        sym_list = list(symbols)[:limit] if limit and limit > 0 else list(symbols)
        u_hash = hashlib.sha256(",".join(sorted(sym_list)).encode("utf-8")).hexdigest()
        now_dt = self.clock.now() if self.clock is not None else datetime.now(UTC)
        now_s = now_dt.strftime("%Y%m%d_%H%M%S")
        r_id = run_id or f"screen_{s_name}_{as_of_date.strftime('%Y%m%d')}_{now_s}"
        res_hash = hashlib.sha256(f"{r_id}:{verdict}:{fail_reason}".encode()).hexdigest()
        skipped = [(s, SkipReason.NO_DATA, f"{s}: Aborted ({fail_reason})") for s in sym_list]
        return ScreenResult(
            name=s_name,
            as_of=as_of_date,
            timeframe=tf,
            rows=[],
            skipped=skipped,
            universe_hash=u_hash,
            result_hash=res_hash,
            provenance={},
            parameters=dict(parameters or {}),
            flags={"adjust": adj, "accept_unverified": accept_unverified},
            verdict=verdict,
            fail_reason=fail_reason,
            run_id=r_id,
            timing={"reads": 0.0, "indicators": 0.0, "rules": 0.0},
        )

    def _run_internal(
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
        group: str | None = None,
    ) -> ScreenResult:
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

        required_inds = _extract_required_indicators(screen)
        max_warmup = _compute_max_warmup(required_inds, self.indicator_resolver)

        lookback_margin = 20
        if self.params is not None:
            try:
                val = self.params.get("screen.lookback_margin_sessions")
                if val is not None:
                    lookback_margin = int(val)
            except Exception:
                pass
        needed_sessions = max(1, max_warmup) + lookback_margin

        # Convert needed_sessions to dates through session resolver or calendar
        if self.session_resolver is not None:
            sessions = self.session_resolver(as_of_date, needed_sessions)
        else:
            sessions = []
            cur = as_of_date
            max_search_days = max(120, needed_sessions * 4)
            cutoff = cur - timedelta(days=max_search_days)
            while cur >= cutoff and len(sessions) < needed_sessions:
                is_trading = False
                if self.calendar is not None:
                    try:
                        is_trading = bool(self.calendar.is_trading_day(cur))
                    except Exception:
                        is_trading = cur.weekday() < 5
                else:
                    is_trading = cur.weekday() < 5
                if is_trading:
                    sessions.append(cur)
                cur -= timedelta(days=1)

        window_start_date = (
            sessions[-1] if sessions else (as_of_date - timedelta(days=needed_sessions * 2))
        )

        # Date boundaries for data fetch (reads end at as-of session close in UTC: no look-ahead)
        end_dt = datetime.combine(as_of_date, time(23, 59, 59), tzinfo=IST).astimezone(UTC)
        start_dt = datetime.combine(window_start_date, time(0, 0), tzinfo=IST).astimezone(UTC)

        bars_by_symbol: dict[str, tuple[Bar, ...]] = {}
        provenance_by_symbol: dict[str, Mapping[str, Any]] = {}
        skipped: list[tuple[str, SkipReason, str]] = []

        t0_reads = perf_counter()
        for idx, sym in enumerate(sym_list, 1):
            if (
                self.progress_every > 0
                and len(sym_list) > self.progress_every
                and (idx % self.progress_every == 0 or idx == len(sym_list))
            ):
                elapsed = perf_counter() - t0_reads
                try:
                    import sys

                    print(
                        f"[screen] {idx}/{len(sym_list)} symbols evaluated ({elapsed:.1f}s)",
                        file=sys.stderr,
                        flush=True,
                    )
                except BrokenPipeError:
                    pass
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
                detail = _format_skip_detail(
                    sym, SkipReason.NO_DATA, f"Data fetch error: {exc}", group=group
                )
                skipped.append((sym, SkipReason.NO_DATA, detail))
                continue

            if isinstance(res, ScreenRefusal):
                detail = _format_skip_detail(sym, res.reason, res.detail, group=group)
                skipped.append((sym, res.reason, detail))
            elif isinstance(res, ScreenBars):
                if not res.bars:
                    detail = _format_skip_detail(
                        sym, SkipReason.NO_DATA, f"No bars found up to {as_of_date}", group=group
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
                            group=group,
                        )
                        skipped.append((sym, SkipReason.NO_DATA, detail))
                    elif max_warmup > 0 and len(bars_up_to_as_of) < max_warmup:
                        detail = _format_skip_detail(
                            sym,
                            SkipReason.INSUFFICIENT_HISTORY,
                            (
                                f"Symbol {sym} has {len(bars_up_to_as_of)} bars, "
                                f"fewer than required warm-up ({max_warmup} bars)"
                            ),
                            group=group,
                        )
                        skipped.append((sym, SkipReason.INSUFFICIENT_HISTORY, detail))
                    else:
                        bars_by_symbol[sym] = bars_up_to_as_of
                        provenance_by_symbol[sym] = res.provenance
            elif isinstance(res, tuple) and len(res) == 2:
                # Tuple variant: (bars, prov) or (None, (reason, detail))
                bars_val, prov_or_refusal = res
                if bars_val is None:
                    if isinstance(prov_or_refusal, tuple) and len(prov_or_refusal) == 2:
                        r_code, r_msg = prov_or_refusal
                        detail = _format_skip_detail(sym, r_code, r_msg, group=group)
                        skipped.append((sym, r_code, detail))
                    else:
                        detail = _format_skip_detail(
                            sym, SkipReason.NO_DATA, str(prov_or_refusal), group=group
                        )
                        skipped.append((sym, SkipReason.NO_DATA, detail))
                else:
                    bars_up_to = tuple(b for b in bars_val if b.end_ts <= end_dt)
                    if not bars_up_to:
                        detail = _format_skip_detail(
                            sym,
                            SkipReason.NO_DATA,
                            f"No completed bars on or before {as_of_date}",
                            group=group,
                        )
                        skipped.append((sym, SkipReason.NO_DATA, detail))
                    elif max_warmup > 0 and len(bars_up_to) < max_warmup:
                        detail = _format_skip_detail(
                            sym,
                            SkipReason.INSUFFICIENT_HISTORY,
                            (
                                f"Symbol {sym} has {len(bars_up_to)} bars, "
                                f"fewer than required warm-up ({max_warmup} bars)"
                            ),
                            group=group,
                        )
                        skipped.append((sym, SkipReason.INSUFFICIENT_HISTORY, detail))
                    else:
                        bars_by_symbol[sym] = bars_up_to
                        provenance_by_symbol[sym] = (
                            prov_or_refusal if isinstance(prov_or_refusal, Mapping) else {}
                        )
        t_reads = perf_counter() - t0_reads

        # Extract required indicators
        required_inds = _extract_required_indicators(screen)

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
                    detail = _format_skip_detail(sym, exc.reason, exc.message, group=group)
                    skipped.append((sym, exc.reason, detail))
                except Exception as exc:
                    detail = _format_skip_detail(
                        sym, SkipReason.INDICATOR_ERROR, str(exc), group=group
                    )
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
                                s,
                                SkipReason.INSUFFICIENT_HISTORY,
                                "Rank metric returned None",
                                group=group,
                            )
                            skipped.append((s, SkipReason.INSUFFICIENT_HISTORY, detail))
                            rows = [r for r in rows if r.symbol != s]
                        else:
                            metric_values[s] = float(m_val)
                    except Exception as exc:
                        detail = _format_skip_detail(
                            s, SkipReason.INDICATOR_ERROR, f"Rank error: {exc}", group=group
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
