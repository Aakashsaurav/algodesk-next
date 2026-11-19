"""Results writer generating session artifacts, metrics, and summary documents."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from algodesk.logs.formatting import format_ist
from algodesk.results.metrics import (
    compute_cagr,
    compute_daily_returns,
    compute_drawdown_metrics,
    compute_exposure_and_turnover,
    compute_round_trip_performance,
    compute_sharpe,
    compute_sortino,
    compute_total_return,
    compute_volatility,
)
from algodesk.results.roundtrips import RoundTrip, compute_round_trip_metrics

logger = logging.getLogger("algodesk.results.writer")


def compute_max_drawdown(equity_curve: Sequence[float]) -> float:
    """Calculate maximum peak-to-trough percentage drawdown from equity series."""
    dd, _ = compute_drawdown_metrics(equity_curve)
    return dd


class ResultsWriter:
    """Writes run artifacts to results/<run_id>/ directory conforming to ADR-031 and ADR-082."""

    def __init__(
        self,
        run_id: str,
        results_dir: Path | str = "results",
        params: Any | None = None,
    ) -> None:
        self.run_id = run_id
        self.results_dir = Path(results_dir)
        self.run_dir = self.results_dir / run_id
        self.params = params

    def write(
        self,
        summary_info: Mapping[str, Any],
        metrics: Mapping[str, Any],
        trades: Sequence[Mapping[str, Any]],
        equity_points: Sequence[Mapping[str, Any]],
        effective_config_text: str,
        registry_snapshot: Mapping[str, Any],
        manifest_info: Mapping[str, Any],
        session_text: str | None = None,
        orders: Sequence[Mapping[str, Any]] | None = None,
    ) -> Path:
        """Write all session artifacts. Writing succeeds even if partial failures occur."""
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.error("Failed to create results directory %s: %s", self.run_dir, exc)
            return self.run_dir

        mode = str(summary_info.get("mode") or manifest_info.get("mode", "paper")).lower()
        is_backtest = mode == "backtest"

        # 1. Round trips and daily equity
        round_trips: list[RoundTrip] = []
        if trades:
            rt_res = compute_round_trip_metrics(trades)
            round_trips = rt_res.get("round_trip_details", [])

        if is_backtest:
            self._write_round_trips_csv(round_trips)
            self._write_equity_daily_csv(equity_points)
            self._write_orders_csv(orders, trades)

        # 2. Enrich metrics for backtest
        enriched_metrics = dict(metrics)
        if is_backtest:
            enriched_metrics = self._enrich_backtest_metrics(
                metrics=enriched_metrics,
                trades=trades,
                round_trips=round_trips,
                equity_points=equity_points,
                summary_info=summary_info,
                registry_snapshot=registry_snapshot,
            )

        # 3. Read readiness.json if present
        readiness_data = self._read_readiness_json()

        # 4. Manifest and Result Hash
        manifest_dict = dict(manifest_info)
        if is_backtest:
            self._update_backtest_manifest(
                manifest=manifest_dict,
                metrics=enriched_metrics,
                trades=trades,
                effective_config_text=effective_config_text,
                registry_snapshot=registry_snapshot,
                readiness_data=readiness_data,
            )

        # 5. Write artifacts
        self._write_summary(summary_info, enriched_metrics, readiness_data, registry_snapshot)
        self._write_metrics(enriched_metrics)
        self._write_trades_csv(trades)
        self._write_equity_csv(equity_points)
        self._write_text_file("config.effective.txt", effective_config_text)
        self._write_json_file("registry.snapshot.json", registry_snapshot)
        self._write_json_file("manifest.json", manifest_dict)
        self._write_session_md(session_text, summary_info, enriched_metrics)

        return self.run_dir

    def _read_readiness_json(self) -> dict[str, Any] | None:
        """Read readiness.json from run directory if present."""
        r_file = self.run_dir / "readiness.json"
        if r_file.is_file():
            try:
                data = json.loads(r_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception as exc:
                logger.debug("Could not read readiness.json: %s", exc)
        return None

    def _enrich_backtest_metrics(
        self,
        metrics: dict[str, Any],
        trades: Sequence[Mapping[str, Any]],
        round_trips: Sequence[RoundTrip],
        equity_points: Sequence[Mapping[str, Any]],
        summary_info: Mapping[str, Any],
        registry_snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Compute comprehensive backtest performance statistics conforming to CP1."""
        res = dict(metrics)
        res["metrics_version"] = 1

        reg_params = registry_snapshot.get("params", {})
        trading_days = int(self._get_param(reg_params, "report.trading_days_per_year", 252))
        rf_pct = float(self._get_param(reg_params, "report.risk_free_rate_pct", 0.0))

        daily_pts = self._extract_daily_equity_points(equity_points)
        daily_equities = [Decimal(str(pt.get("equity", 0.0))) for pt in daily_pts]
        timestamps = [self._parse_ts(pt.get("ts")) for pt in daily_pts]
        start_eq = daily_equities[0] if daily_equities else Decimal("0.00")
        end_eq = daily_equities[-1] if daily_equities else start_eq

        self._calc_returns_and_ratios(
            res, daily_equities, timestamps, start_eq, end_eq, trading_days, rf_pct
        )

        rt_perf = compute_round_trip_performance(round_trips)
        res["win_rate"] = rt_perf["win_rate"]
        res["expectancy"] = float(rt_perf["expectancy"])
        res["profit_factor"] = rt_perf["profit_factor"]
        res["closed_round_trips"] = rt_perf["closed_round_trips"]

        self._calc_exposure_and_turnover(res, daily_pts, trades, start_eq, trading_days)

        oos_from_str = summary_info.get("oos_from")
        if oos_from_str:
            if trades or daily_pts:
                self._apply_oos_split(
                    res, str(oos_from_str), trades, daily_pts, trading_days, rf_pct
                )
            else:
                res["oos_from"] = str(oos_from_str)

        study_name = summary_info.get("study")
        if study_name:
            res["study"] = str(study_name)
            res["trial"] = int(summary_info.get("trial", 1))

        return res

    def _calc_returns_and_ratios(
        self,
        res: dict[str, Any],
        daily_equities: Sequence[Decimal],
        timestamps: Sequence[datetime | None],
        start_eq: Decimal,
        end_eq: Decimal,
        trading_days: int,
        rf_pct: float,
    ) -> None:
        """Compute return, cagr, volatility, sharpe, sortino, and max drawdown."""
        d_returns = compute_daily_returns(daily_equities)
        cal_days = 0.0
        if len(timestamps) >= 2 and timestamps[0] and timestamps[-1]:
            cal_days = max(1.0, (timestamps[-1].date() - timestamps[0].date()).days)

        res["total_return"] = compute_total_return(start_eq, end_eq)
        res["cagr"] = compute_cagr(start_eq, end_eq, calendar_days=cal_days)
        res["volatility"] = compute_volatility(d_returns, trading_days_per_year=trading_days)
        res["sharpe"] = compute_sharpe(
            d_returns, risk_free_rate_pct=rf_pct, trading_days_per_year=trading_days
        )
        res["sortino"] = compute_sortino(
            d_returns, risk_free_rate_pct=rf_pct, trading_days_per_year=trading_days
        )

        valid_ts = [t for t in timestamps if t is not None]
        max_dd, longest_dd_days = compute_drawdown_metrics(
            daily_equities, timestamps=valid_ts if len(valid_ts) == len(daily_equities) else None
        )
        res["max_drawdown"] = max_dd
        res["longest_drawdown_days"] = longest_dd_days

    def _calc_exposure_and_turnover(
        self,
        res: dict[str, Any],
        daily_pts: Sequence[Mapping[str, Any]],
        trades: Sequence[Mapping[str, Any]],
        start_eq: Decimal,
        trading_days: int,
    ) -> None:
        """Compute exposure, turnover, and annualized turnover."""
        session_positions = [abs(float(pt.get("unrealized", 0.0))) > 1e-4 for pt in daily_pts]
        total_traded_val = sum(
            Decimal(str(tr.get("price", 0.0))) * Decimal(str(tr.get("qty", 0))) for tr in trades
        )
        exp, ann_turn = compute_exposure_and_turnover(
            session_has_position=session_positions,
            total_traded_value=total_traded_val,
            start_equity=start_eq,
            trading_days_per_year=trading_days,
        )
        res["exposure"] = exp
        res["annualized_turnover"] = ann_turn
        res["turnover"] = round(float(total_traded_val / start_eq) if start_eq > 0 else 0.0, 4)

    def _apply_oos_split(
        self,
        res: dict[str, Any],
        oos_from_str: str,
        trades: Sequence[Mapping[str, Any]],
        daily_pts: Sequence[Mapping[str, Any]],
        trading_days: int,
        rf_pct: float,
    ) -> None:
        """Partition performance into in_sample and out_of_sample metrics."""
        res["oos_from"] = oos_from_str
        is_trades = [
            tr for tr in trades if str(tr.get("ts_ist", tr.get("ts", "")))[:10] < oos_from_str
        ]
        oos_trades = [
            tr for tr in trades if str(tr.get("ts_ist", tr.get("ts", "")))[:10] >= oos_from_str
        ]

        is_pts = [p for p in daily_pts if str(p.get("ts_ist", p.get("ts", "")))[:10] < oos_from_str]
        oos_pts = [
            p for p in daily_pts if str(p.get("ts_ist", p.get("ts", "")))[:10] >= oos_from_str
        ]

        res["in_sample"] = self._compute_sub_metrics(is_trades, is_pts, trading_days, rf_pct)
        res["out_of_sample"] = self._compute_sub_metrics(oos_trades, oos_pts, trading_days, rf_pct)

    def _compute_sub_metrics(
        self,
        trades: Sequence[Mapping[str, Any]],
        pts: Sequence[Mapping[str, Any]],
        trading_days: int,
        rf_pct: float,
    ) -> dict[str, Any]:
        """Compute subset metrics for split period."""
        equities = [Decimal(str(p.get("equity", 0.0))) for p in pts]
        rts = compute_round_trip_metrics(trades).get("round_trip_details", []) if trades else []
        rt_perf = compute_round_trip_performance(rts)
        returns = compute_daily_returns(equities)
        s_eq = equities[0] if equities else Decimal("0.00")
        e_eq = equities[-1] if equities else s_eq
        net_pnl = float(e_eq - s_eq)
        max_dd, _ = compute_drawdown_metrics(equities)

        return {
            "net_pnl": round(net_pnl, 2),
            "trades": len(trades),
            "closed_round_trips": rt_perf["closed_round_trips"],
            "win_rate": rt_perf["win_rate"],
            "profit_factor": rt_perf["profit_factor"],
            "max_drawdown": max_dd,
            "sharpe": compute_sharpe(
                returns, risk_free_rate_pct=rf_pct, trading_days_per_year=trading_days
            ),
        }

    def _update_backtest_manifest(
        self,
        manifest: dict[str, Any],
        metrics: dict[str, Any],
        trades: Sequence[Mapping[str, Any]],
        effective_config_text: str,
        registry_snapshot: Mapping[str, Any],
        readiness_data: dict[str, Any] | None,
    ) -> None:
        """Compute result_hash covering all 8 components defined in FR-BKT-6 and ADR-082."""
        u_hash = ""
        if readiness_data and "universe" in readiness_data:
            u_hash = str(readiness_data["universe"].get("hash", ""))
        manifest["universe_hash"] = u_hash

        data_checksums = manifest.get("checksums") or manifest.get("data_files") or []
        manifest["dataset_checksums"] = data_checksums

        action_hash = manifest.get("action_hash", "none")
        manifest["action_hash"] = action_hash

        pv = manifest.get("plugin_versions", {})
        fee_hash = pv.get("fees", {}).get("hash", "default")
        manifest["fee_schedule_hash"] = fee_hash

        strat_hash = pv.get("strategy", {}).get("hash", "strategy_0")
        manifest["strategy_file_hash"] = strat_hash

        strat_params: dict[str, Any] = {}
        for line in effective_config_text.splitlines():
            if "=" in line and not line.startswith("[") and not line.startswith("#"):
                k, v = line.split("=", 1)
                strat_params[k.strip()] = v.strip()
        manifest["strategy_parameters"] = strat_params

        reg_hash = str(registry_snapshot.get("hash", "core"))
        manifest["registry_snapshot_hash"] = reg_hash

        reg_params = registry_snapshot.get("params", {})
        manifest["model_settings"] = {
            "slippage_bps": self._get_param(reg_params, "backtest.slippage_bps", 5),
            "delay_bars": self._get_param(reg_params, "backtest.delay_bars", 1),
            "participation_pct": self._get_param(reg_params, "backtest.participation_pct", 10),
            "price_tick": self._get_param(reg_params, "backtest.price_tick", 0.01),
        }

        # Canonical result_hash payload
        payload = {
            "universe_hash": manifest["universe_hash"],
            "dataset_checksums": manifest["dataset_checksums"],
            "action_hash": manifest["action_hash"],
            "fee_schedule_hash": manifest["fee_schedule_hash"],
            "strategy_file_hash": manifest["strategy_file_hash"],
            "strategy_parameters": manifest["strategy_parameters"],
            "registry_snapshot_hash": manifest["registry_snapshot_hash"],
            "model_settings": manifest["model_settings"],
            "seed": manifest.get("seed"),
            "code_version": manifest.get("code_version", "0.1.0"),
        }
        res_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        manifest["result_hash"] = res_hash

    def _write_summary(
        self,
        summary_info: Mapping[str, Any],
        metrics: Mapping[str, Any],
        readiness_data: dict[str, Any] | None = None,
        registry_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        """Generate SUMMARY.md keeping strictly within the 150-word budget."""
        run_name = summary_info.get("run_name", self.run_id)
        mode = summary_info.get("mode", "paper")
        is_backtest = str(mode).lower() == "backtest"

        trade_count = int(metrics.get("trades", 0))
        net_pnl = float(metrics.get("net_pnl", 0.0))
        gross_pnl = float(metrics.get("gross_pnl", 0.0))
        charges = float(metrics.get("charges", 0.0))
        win_rate = float(metrics.get("win_rate", 0.0))
        max_dd = float(metrics.get("max_drawdown", 0.0))
        verdict = summary_info.get("verdict", "PASS")
        fail_reason = summary_info.get("fail_reason")
        fail_clause = f"Reason: {fail_reason}. " if fail_reason else ""
        start_ist = summary_info.get("start_ist") or metrics.get("first_tick_ist")
        start_clause = f"Start: {start_ist}. " if start_ist and start_ist != "N/A" else ""
        closed_rt = int(metrics.get("closed_round_trips", 0))

        open_positions = metrics.get("open_positions", [])
        open_clause = ""
        open_note = ""
        if open_positions:
            pos_summary = ", ".join(f"{p['instrument']} (qty={p['qty']})" for p in open_positions)
            open_note = f"Unresolved open positions at stop: {pos_summary}. "
            open_clause = f"Open at stop: {pos_summary}. "

        lead_parts: list[str] = []
        reg_params = (registry_snapshot or {}).get("params", {})
        trades_floor = reg_params.get("report.min_trades")
        if trades_floor is None and self.params is not None:
            trades_floor = self.params.get("report.min_trades")
        if trades_floor is None:
            trades_floor = 0
        required_trades = int(trades_floor)
        if is_backtest and required_trades > 0 and trade_count < required_trades:
            lead_parts.append("Statistics are not statistically meaningful due to low trade count.")

        if is_backtest and "out_of_sample" in metrics:
            oos = metrics["out_of_sample"]
            oos_date = metrics.get("oos_from", "N/A")
            lead_parts.append(
                f"Out-of-sample (from {oos_date}): Net P&L: ₹{oos.get('net_pnl', 0.0):.2f}, "
                f"win rate: {oos.get('win_rate', 0.0):.1f}%, Sharpe: {oos.get('sharpe', 0.0):.2f}."
            )

        date_str = summary_info.get("date", "2026-10-26")
        main_clause = (
            f"AlgoDesk Next {mode} session '{run_name}' executed on {date_str} IST. {start_clause}"
            f"Net P&L: ₹{net_pnl:.2f} (Gross: ₹{gross_pnl:.2f}, Charges: ₹{charges:.2f}) "
            f"across {closed_rt} closed round trips ({trade_count} trades) with "
            f"{win_rate:.1f}% win rate and max DD of {max_dd:.2f}%. Verdict: {verdict}. "
            f"{fail_clause}{open_clause}"
        )

        if is_backtest:
            assumptions = self._build_backtest_assumptions(metrics, readiness_data)
            disclaimer = f"Note: {open_note}{assumptions}\n"
        else:
            disclaimer = f"Note: {open_note}Simulated paper fills assume zero impact.\n"

        lead_str = (" ".join(lead_parts) + " ") if lead_parts else ""
        body = f"{lead_str}{main_clause}{disclaimer}"

        # Word limit check (enforce <= 150 words)
        words = body.split()
        if len(words) > 150:
            body = " ".join(words[:145]) + "...\n"

        content = f"# Run Summary: {self.run_id}\n\n## Summary\n{body}"
        self._write_text_file("SUMMARY.md", content)

    def _build_backtest_assumptions(
        self,
        metrics: Mapping[str, Any],
        readiness_data: dict[str, Any] | None,
    ) -> str:
        """Construct the standardized assumptions line for backtest summaries."""
        coverage_pct = 100.0
        d_range = "full"
        if readiness_data:
            cov = readiness_data.get("universe", {}).get("coverage_pct")
            if cov is not None:
                coverage_pct = float(cov)
            rng = readiness_data.get("range", {})
            if rng.get("start") and rng.get("end"):
                d_range = f"{rng['start']} to {rng['end']}"

        trial_str = ""
        if "trial" in metrics:
            trial_str = f", trial {metrics['trial']} of study {metrics.get('study', 'default')}"

        oos_str = "none"
        if "out_of_sample" in metrics:
            oos_net = metrics["out_of_sample"].get("net_pnl", 0.0)
            oos_str = f"Net ₹{oos_net:.2f}"

        return (
            "Assumptions: fill model (bar synth with slippage), price basis (splits), "
            f"dividends not credited, fee schedule (zerodha_eq), data range ({d_range}), "
            f"universe coverage ({coverage_pct:.0f}%){trial_str}, "
            f"out-of-sample ({oos_str}), survivorship note (point-in-time universe unadjusted)."
        )

    def _write_equity_daily_csv(self, equity_points: Sequence[Mapping[str, Any]]) -> None:
        """Serialize daily mark-to-market equity snapshots to equity_daily.csv."""
        daily_pts = self._extract_daily_equity_points(equity_points)
        fieldnames = [
            "date",
            "equity",
            "cash",
            "unrealized",
            "realized",
            "drawdown",
            "daily_return",
        ]
        file_path = self.run_dir / "equity_daily.csv"
        prev_eq: float | None = None
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for pt in daily_pts:
                    eq = float(pt.get("equity", 0.0))
                    d_return = 0.0
                    if prev_eq is not None and prev_eq > 0.0:
                        d_return = round((eq - prev_eq) / prev_eq, 6)
                    prev_eq = eq

                    ts_str = str(pt.get("ts_ist", pt.get("ts", "")))[:10]
                    writer.writerow(
                        {
                            "date": ts_str,
                            "equity": eq,
                            "cash": float(pt.get("cash", 0.0)),
                            "unrealized": float(pt.get("unrealized", 0.0)),
                            "realized": float(pt.get("realized", 0.0)),
                            "drawdown": float(pt.get("drawdown", 0.0)),
                            "daily_return": d_return,
                        }
                    )
        except Exception as exc:
            logger.warning("Failed writing equity_daily.csv: %s", exc)

    def _write_round_trips_csv(self, round_trips: Sequence[RoundTrip]) -> None:
        """Serialize matched round-trip trades to round_trips.csv."""
        fieldnames = [
            "round_trip_id",
            "instrument",
            "entry_date",
            "exit_date",
            "open_side",
            "qty",
            "open_price",
            "close_price",
            "gross_pnl",
            "charges",
            "net_pnl",
        ]
        file_path = self.run_dir / "round_trips.csv"
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for idx, rt in enumerate(round_trips, 1):
                    writer.writerow(
                        {
                            "round_trip_id": f"RT-{idx}",
                            "instrument": rt.instrument,
                            "entry_date": rt.entry_date,
                            "exit_date": rt.exit_date,
                            "open_side": rt.open_side,
                            "qty": rt.qty,
                            "open_price": rt.open_price,
                            "close_price": rt.close_price,
                            "gross_pnl": rt.gross_pnl,
                            "charges": rt.charges,
                            "net_pnl": rt.net_pnl,
                        }
                    )
        except Exception as exc:
            logger.warning("Failed writing round_trips.csv: %s", exc)

    def _write_orders_csv(
        self,
        orders: Sequence[Mapping[str, Any]] | None,
        trades: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        """Serialize session orders to orders.csv."""
        fieldnames = [
            "client_ref",
            "venue_ref",
            "instrument",
            "side",
            "order_type",
            "qty",
            "price",
            "trigger_price",
            "product",
            "state",
            "filled_qty",
            "avg_price",
            "created_ts",
            "created_ts_ist",
        ]
        file_path = self.run_dir / "orders.csv"
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                ord_list = list(orders) if orders else []
                if not ord_list and trades:
                    ord_list = [
                        {
                            "client_ref": tr.get("client_ref", f"ORD-{idx}"),
                            "venue_ref": tr.get("fill_id", ""),
                            "instrument": tr.get("instrument", ""),
                            "side": tr.get("side", ""),
                            "order_type": "MARKET",
                            "qty": tr.get("qty", 0),
                            "price": tr.get("price", 0.0),
                            "trigger_price": "",
                            "product": "CNC",
                            "state": "FILLED",
                            "filled_qty": tr.get("qty", 0),
                            "avg_price": tr.get("price", 0.0),
                            "created_ts": tr.get("ts", ""),
                            "created_ts_ist": tr.get("ts_ist", ""),
                        }
                        for idx, tr in enumerate(trades, 1)
                    ]
                for ord_obj in ord_list:
                    row = self._format_order_row(ord_obj, fieldnames)
                    writer.writerow(row)
        except Exception as exc:
            logger.warning("Failed writing orders.csv: %s", exc)

    def _format_order_row(
        self,
        ord_obj: Mapping[str, Any] | Any,
        fieldnames: Sequence[str],
    ) -> dict[str, Any]:
        """Normalize an Order dataclass or dict into CSV row."""
        res: dict[str, Any] = {}
        if isinstance(ord_obj, Mapping):
            for k in fieldnames:
                res[k] = ord_obj.get(k, "")
            return res

        obj: Any = ord_obj
        res["client_ref"] = getattr(obj, "client_ref", "")
        res["venue_ref"] = getattr(obj, "venue_ref", "") or ""
        spec = getattr(obj, "spec", None)
        if spec:
            res["instrument"] = str(getattr(spec, "instrument", ""))
            side = getattr(spec, "side", "")
            res["side"] = side.value if hasattr(side, "value") else str(side)
            order_type = getattr(spec, "type", "")
            res["order_type"] = (
                order_type.value if hasattr(order_type, "value") else str(order_type)
            )
            res["qty"] = getattr(spec, "qty", 0)
            price = getattr(spec, "price", None)
            res["price"] = float(price) if price else ""
            trig = getattr(spec, "trigger_price", None)
            res["trigger_price"] = float(trig) if trig else ""
            prod = getattr(spec, "product", "")
            res["product"] = prod.value if hasattr(prod, "value") else str(prod)
        state = getattr(obj, "state", "")
        res["state"] = state.value if hasattr(state, "value") else str(state)
        res["filled_qty"] = getattr(obj, "filled_qty", 0)
        res["avg_price"] = float(getattr(obj, "avg_price", 0))
        cts = getattr(obj, "created_ts", None)
        res["created_ts"] = cts.isoformat() if cts else ""
        res["created_ts_ist"] = format_ist(cts) if cts else ""
        return res

    def _extract_daily_equity_points(
        self,
        equity_points: Sequence[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        """Filter equity snapshots down to one distinct mark per trading day."""
        by_date: dict[str, Mapping[str, Any]] = {}
        for pt in equity_points:
            ts_str = str(pt.get("ts_ist", pt.get("ts", "")))[:10]
            if ts_str:
                by_date[ts_str] = pt
        return [by_date[d] for d in sorted(by_date.keys())]

    def _get_param(self, reg_params: Mapping[str, Any], name: str, default: Any) -> Any:
        if name in reg_params:
            return reg_params[name]
        if self.params:
            val = self.params.get(name)
            if val is not None:
                return val
        return default

    def _parse_ts(self, ts_val: Any) -> datetime | None:
        if isinstance(ts_val, datetime):
            return ts_val.astimezone(UTC)
        if isinstance(ts_val, str) and ts_val:
            try:
                return datetime.fromisoformat(ts_val.replace("Z", "+00:00")).astimezone(UTC)
            except Exception:
                return None
        return None

    def _write_metrics(self, metrics: Mapping[str, Any]) -> None:
        """Serialize session metrics to metrics.json."""
        default_metrics = {
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "charges": 0.0,
            "trades": 0,
            "win_rate": 0.0,
            "gross_win_rate": 0.0,
            "closed_round_trips": 0,
            "open_positions": [],
            "max_drawdown": 0.0,
            "exposure_time": 0.0,
            # pyrefly: ignore[implicit-any-empty-container]
            "rejects_by_rule": {},
            # pyrefly: ignore[implicit-any-empty-container]
            "ticks_per_instrument": {},
            "feed_gaps": 0,
            "killswitch_events": 0,
        }
        merged = {**default_metrics, **metrics}
        self._write_json_file("metrics.json", merged)

    def _write_trades_csv(self, trades: Sequence[Mapping[str, Any]]) -> None:
        """Serialize trade fills to trades.csv."""
        fieldnames = [
            "trade_id",
            "fill_id",
            "client_ref",
            "instrument",
            "side",
            "qty",
            "price",
            "charges",
            "ts",
            "ts_ist",
        ]
        file_path = self.run_dir / "trades.csv"
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for tr in trades:
                    row = {k: tr.get(k, "") for k in fieldnames}
                    if not row.get("ts_ist") and row.get("ts"):
                        row["ts_ist"] = format_ist(row["ts"])
                    writer.writerow(row)
        except Exception as exc:
            logger.warning("Failed writing trades.csv: %s", exc)

    def _write_equity_csv(self, equity_points: Sequence[Mapping[str, Any]]) -> None:
        """Serialize equity curve snapshots to equity.csv."""
        fieldnames = ["ts", "equity", "cash", "unrealized", "realized", "drawdown", "ts_ist"]
        file_path = self.run_dir / "equity.csv"
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for pt in equity_points:
                    row = {k: pt.get(k, "") for k in fieldnames}
                    if not row.get("ts_ist") and row.get("ts"):
                        row["ts_ist"] = format_ist(row["ts"])
                    writer.writerow(row)
        except Exception as exc:
            logger.warning("Failed writing equity.csv: %s", exc)

    def _write_session_md(
        self,
        session_text: str | None,
        summary_info: Mapping[str, Any],
        metrics: Mapping[str, Any],
    ) -> None:
        """Write session summary text for notifications and auditing."""
        if session_text:
            content = session_text
        else:
            run_name = summary_info.get("run_name", self.run_id)
            net_pnl = float(metrics.get("net_pnl", 0.0))
            trades = int(metrics.get("trades", 0))
            max_dd = metrics.get("max_drawdown", 0.0)
            first_tick_ist = metrics.get("first_tick_ist", "N/A")
            content = (
                f"Session {run_name} completed.\n"
                f"Start (IST): {first_tick_ist}\n"
                f"Net P&L: ₹{net_pnl:.2f} | Trades: {trades} | Max DD: {max_dd:.2f}%\n"
                f"Status: {summary_info.get('verdict', 'COMPLETED')}\n"
            )
        self._write_text_file("session.md", content)

    def _write_text_file(self, filename: str, content: str) -> None:
        try:
            (self.run_dir / filename).write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed writing %s: %s", filename, exc)

    def _write_json_file(self, filename: str, data: Any) -> None:
        try:
            (self.run_dir / filename).write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Failed writing %s: %s", filename, exc)
