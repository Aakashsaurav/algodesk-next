## File: `./__init__.py`

```python
from screener.base import (
    ScreenerConfig,
    ScreenResult,
    ScanSummary,
    TickData,
    ScreenMode,
    SignalDirection,
    RankBy,
    ExportFormat,
)
from screener.engine import ScreenerEngine
from screener.universe import Universe
from screener.filters import DataValidator, PreFilter
from screener.scoring import Scorer, ScoreMode
from screener.output import OutputFormatter, ScreenerHistory

__all__ = [
    "ScreenerConfig",
    "ScreenResult",
    "ScanSummary",
    "TickData",
    "ScreenMode",
    "SignalDirection",
    "RankBy",
    "ExportFormat",
    "ScreenerEngine",
    "Universe",
    "DataValidator",
    "PreFilter",
    "Scorer",
    "ScoreMode",
    "OutputFormatter",
    "ScreenerHistory",
]

```

---

## File: `./base.py`

```python
"""
screener/base.py
----------------
Core data classes, enums, and contracts for the unified screener module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd


class ScreenMode(Enum):
    EOD = "EOD"
    HISTORICAL = "HISTORICAL"
    LIVE = "LIVE"


class SignalDirection(Enum):
    BULLISH = 1
    BEARISH = -1
    NEUTRAL = 0
    ANY = 2


class RankBy(Enum):
    SCORE = "SCORE"
    CLOSE = "CLOSE"
    VOLUME = "VOLUME"
    ATR_PCT = "ATR_PCT"
    RS_SCORE = "RS_SCORE"
    SIGNAL_STRENGTH = "SIGNAL_STRENGTH"


class ExportFormat(Enum):
    CSV = "CSV"
    JSON = "JSON"
    BOTH = "BOTH"
    NONE = "NONE"


@dataclass
class ScreenerConfig:
    # Identity
    scan_name: str = "scan"
    mode: ScreenMode = ScreenMode.EOD

    # Universe — accepts any symbol list
    symbols: list[str] = field(default_factory=list)

    # Parallelism
    n_workers: int = 8
    timeout_per_symbol: float = 30.0

    # Scoring
    use_weighted_scoring: bool = False
    rule_weights: dict[str, float] = field(default_factory=dict)

    # Pre-filters (applied before rules)
    min_bars: int = 50
    min_price: float = 0.0
    max_price: float = 0.0
    min_volume: float = 0.0
    min_atr_pct: float = 0.0

    # Output
    max_results: int = 0          # 0 = unlimited
    rank_by: RankBy = RankBy.SCORE
    rank_ascending: bool = False
    export_format: ExportFormat = ExportFormat.CSV
    export_dir: Path = Path("screener/output")

    # Historical mode
    date_range: tuple[str, str] | None = None    # (from_date, to_date)

    # Live mode
    tick_buffer_size: int = 1000   # max ticks per symbol in memory
    min_historical_bars: int = 200  # bars to pre-load before live scan

    def __post_init__(self):
        if self.n_workers <= 0:
            self.n_workers = 1
        if self.min_bars <= 0:
            self.min_bars = 1

    def validate(self) -> list[str]:
        errors = []
        if not self.symbols:
            errors.append("symbols list cannot be empty")
        if self.n_workers <= 0:
            errors.append("n_workers must be > 0")
        if self.min_bars <= 0:
            errors.append("min_bars must be > 0")
        if self.timeout_per_symbol <= 0:
            errors.append("timeout_per_symbol must be > 0")
        if self.mode == ScreenMode.HISTORICAL and not self.date_range:
            errors.append("date_range is required in HISTORICAL mode")
        return errors


@dataclass(slots=True)
class RuleResult:
    rule_name: str
    passed: bool
    value: float | None          # computed indicator value
    threshold: float | None      # the threshold compared against
    details: dict[str, Any]      # arbitrary metadata
    weight: float = 1.0          # for weighted scoring


@dataclass
class ScreenResult:
    symbol: str
    scan_date: str               # YYYY-MM-DD
    scan_time: str               # HH:MM:SS
    mode: ScreenMode

    # Rule results
    rules_passed: int
    rules_total: int
    passed: bool                  # rules_passed == rules_total

    # Scoring
    score: float                  # 0.0 to 1.0
    signal_direction: SignalDirection

    # Market snapshot at time of scan
    close: float
    volume: float
    atr_pct: float | None

    # Per-rule detail
    rule_details: dict[str, RuleResult]

    # Indicator values at signal bar
    indicator_values: dict[str, float]

    # Live mode extras
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    depth: dict | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "scan_date": self.scan_date,
            "scan_time": self.scan_time,
            "mode": self.mode.value,
            "rules_passed": self.rules_passed,
            "rules_total": self.rules_total,
            "passed": self.passed,
            "score": self.score,
            "signal_direction": self.signal_direction.name,
            "close": self.close,
            "volume": self.volume,
            "atr_pct": self.atr_pct,
            "rule_details": {
                k: {
                    "passed": v.passed,
                    "value": v.value,
                    "threshold": v.threshold,
                    "details": v.details,
                    "weight": v.weight
                } for k, v in self.rule_details.items()
            },
            "indicator_values": self.indicator_values,
            "ltp": self.ltp,
            "bid": self.bid,
            "ask": self.ask,
            "depth": self.depth
        }

    def to_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "scan_date": self.scan_date,
            "scan_time": self.scan_time,
            "mode": self.mode.value,
            "rules_passed": self.rules_passed,
            "rules_total": self.rules_total,
            "passed": self.passed,
            "score": self.score,
            "signal_direction": self.signal_direction.name,
            "close": self.close,
            "volume": self.volume,
            "atr_pct": self.atr_pct,
            "ltp": self.ltp,
            "bid": self.bid,
            "ask": self.ask
        }

    def passed_rules(self) -> list[str]:
        return [k for k, v in self.rule_details.items() if v.passed]

    def failed_rules(self) -> list[str]:
        return [k for k, v in self.rule_details.items() if not v.passed]


@dataclass
class ScanSummary:
    scan_name: str
    mode: ScreenMode
    scan_date: str
    elapsed_seconds: float
    symbols_scanned: int
    symbols_passed: int
    symbols_failed: int
    symbols_errored: int
    results: list[ScreenResult]

    def to_dict(self) -> dict:
        return {
            "scan_name": self.scan_name,
            "mode": self.mode.value,
            "scan_date": self.scan_date,
            "elapsed_seconds": self.elapsed_seconds,
            "symbols_scanned": self.symbols_scanned,
            "symbols_passed": self.symbols_passed,
            "symbols_failed": self.symbols_failed,
            "symbols_errored": self.symbols_errored,
            "results": [r.to_dict() for r in self.results]
        }

    def top_n(self, n: int) -> list[ScreenResult]:
        if n <= 0:
            return []
        return self.results[:n]

    def filter_by_direction(self, direction: SignalDirection) -> list[ScreenResult]:
        return [r for r in self.results if r.signal_direction == direction]


@dataclass(slots=True)
class TickData:
    symbol: str
    timestamp: pd.Timestamp
    ltp: float
    open: float
    high: float
    low: float
    close: float
    volume: int
    # Market depth (5 levels)
    bid_prices: list[float]
    bid_quantities: list[int]
    ask_prices: list[float]
    ask_quantities: list[int]
    # Derived
    bid_ask_spread: float
    total_bid_qty: int
    total_ask_qty: int

    def to_ohlcv_row(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume
        }

    def validate(self) -> bool:
        if self.ltp <= 0:
            return False
        if len(self.bid_prices) != 5:
            return False
        return True

```

---

## File: `./engine.py`

```python
"""
screener/engine.py
------------------
The core execution engine for the unified screener.
"""

__all__ = ["ScreenerEngine"]

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from datetime import datetime
from core.clock import clock_now

import pandas as pd

from screener.base import (
    ScreenMode, ScreenerConfig, ScreenResult, ScanSummary, TickData,
    SignalDirection, RankBy
)
from screener.rules.base import ScreenRule
from screener.filters import PreFilter, DataValidator
from screener.scoring import Scorer, ScoreMode

logger = logging.getLogger(__name__)


class ScreenerEngine:
    def __init__(
        self,
        config: ScreenerConfig,
        rule: ScreenRule,
        pre_filter: PreFilter | None = None,
        scorer: Scorer | None = None
    ):
        self.config = config
        self.rule = rule
        self.pre_filter = pre_filter or PreFilter()
        self.scorer = scorer or Scorer(ScoreMode.HIT_COUNT)
        self.validator = DataValidator()
        from indicators.engine import IndicatorEngine
        self._engine = IndicatorEngine()

    def _evaluate_symbol(self, symbol: str, df: pd.DataFrame, scan_date: str, scan_time: str) -> ScreenResult | None:
        """
        Evaluate a single symbol's DataFrame against the pre-filter and rule tree.
        """
        # Validate data
        is_valid, err = self.validator.validate(symbol, df)
        if not is_valid:
            logger.debug("[%s] Validation failed: %s", symbol, err)
            return None

        # Apply pre-filters
        passed_filter, filter_err = self.pre_filter.apply(symbol, df)
        if not passed_filter:
            logger.debug("[%s] Pre-filter failed: %s", symbol, filter_err)
            return None

        # Evaluate rules
        rule_res = self.rule.evaluate_safe(df)
        
        # We need to flatten composite rules to count passed/total.
        # But for now, we just pass the top-level details to the scorer.
        # If the top rule is a composite, its 'details' contains the sub-rules.
        # Let's extract the sub-rules for scoring.
        rule_details = {}
        if hasattr(self.rule, "rules"): # Composite rule
            rule_details = rule_res.details
        else:
            rule_details = {self.rule.name: rule_res}

        score = self.scorer.score(rule_details, self.config.rule_weights)
        
        passed_count = sum(1 for r in rule_details.values() if r.passed)
        total_count = len(rule_details)

        # Extract market snapshot
        close = float(df["close"].iloc[-1])
        volume = float(df["volume"].iloc[-1])
        
        # Calculate ATR% if possible
        atr_pct = None
        if len(df) >= 14:
            try:
                atr_df = self._engine.compute("atr", df)
                atr_val = atr_df["atr"].iloc[-1]
                atr_pct = (atr_val / close) * 100
            except Exception:
                pass

        # Determine signal direction
        direction = getattr(self.rule, "signal_direction", SignalDirection.BULLISH)

        result = ScreenResult(
            symbol=symbol,
            scan_date=scan_date,
            scan_time=scan_time,
            mode=self.config.mode,
            rules_passed=passed_count,
            rules_total=total_count,
            passed=rule_res.passed,
            score=score,
            signal_direction=direction,
            close=close,
            volume=volume,
            atr_pct=atr_pct,
            rule_details=rule_details,
            indicator_values={} # Can be populated if needed
        )
        return result

    def run_eod(self, data_dict: dict[str, pd.DataFrame]) -> ScanSummary:
        """
        Run EOD scan across all symbols in parallel.
        """
        start_time = time.monotonic()
        now = clock_now()
        scan_date = now.strftime("%Y-%m-%d")
        scan_time = now.strftime("%H:%M:%S")

        results: list[ScreenResult] = []
        errored = 0

        with ThreadPoolExecutor(max_workers=self.config.n_workers) as executor:
            futures = {
                executor.submit(self._evaluate_symbol, sym, df, scan_date, scan_time): sym
                for sym, df in data_dict.items() if sym in self.config.symbols
            }

            for future in as_completed(futures):
                try:
                    res = future.result(timeout=self.config.timeout_per_symbol)
                    if res is not None:
                        results.append(res)
                except FuturesTimeoutError:
                    sym = futures[future]
                    logger.error("Timeout evaluating %s", sym)
                    errored += 1
                except Exception as e:
                    sym = futures[future]
                    logger.error("Error evaluating %s: %s", sym, e)
                    errored += 1

        elapsed = time.monotonic() - start_time
        
        passed_count = sum(1 for r in results if r.passed)
        failed_count = len(results) - passed_count

        # Apply ranking
        ranked_results = self.scorer.rank(results, self.config.rank_by, self.config.rank_ascending)

        # Apply max_results
        if self.config.max_results > 0:
            ranked_results = ranked_results[:self.config.max_results]

        return ScanSummary(
            scan_name=self.config.scan_name,
            mode=ScreenMode.EOD,
            scan_date=scan_date,
            elapsed_seconds=elapsed,
            symbols_scanned=len(futures),
            symbols_passed=passed_count,
            symbols_failed=failed_count,
            symbols_errored=errored,
            results=ranked_results
        )

    def run_historical(self, data_dict: dict[str, pd.DataFrame]) -> ScanSummary:
        """
        Run a sliding window backtest across the requested date range.
        """
        if not self.config.date_range:
            raise ValueError("date_range must be set in config for HISTORICAL mode")

        start_date, end_date = self.config.date_range
        start_time = time.monotonic()
        
        all_results = []
        errored = 0
        symbols_scanned = 0

        with ThreadPoolExecutor(max_workers=self.config.n_workers) as executor:
            futures = []
            for sym, df in data_dict.items():
                if sym not in self.config.symbols:
                    continue
                symbols_scanned += 1
                # Submit symbol to be evaluated for its history
                futures.append(executor.submit(self._evaluate_historical_symbol, sym, df, start_date, end_date))

            for future in as_completed(futures):
                try:
                    res_list, err_count = future.result(timeout=self.config.timeout_per_symbol)
                    all_results.extend(res_list)
                    errored += err_count
                except FuturesTimeoutError:
                    logger.error("Timeout evaluating historical symbol")
                    errored += 1
                except Exception as e:
                    logger.error("Historical error: %s", e)
                    errored += 1

        passed_count = sum(1 for r in all_results if r.passed)

        ranked_results = self.scorer.rank(all_results, self.config.rank_by, self.config.rank_ascending)

        if self.config.max_results > 0:
            ranked_results = ranked_results[:self.config.max_results]

        elapsed = time.monotonic() - start_time
        
        return ScanSummary(
            scan_name=self.config.scan_name,
            mode=ScreenMode.HISTORICAL,
            scan_date=f"{start_date} to {end_date}",
            elapsed_seconds=elapsed,
            symbols_scanned=symbols_scanned,
            symbols_passed=passed_count,
            symbols_failed=0,
            symbols_errored=errored,
            results=ranked_results
        )

    def _evaluate_historical_symbol(self, sym: str, df: pd.DataFrame, start_date: str, end_date: str) -> tuple[list[ScreenResult], int]:
        """Evaluate a single symbol over its historical dates."""
        res_list = []
        err_count = 0
        
        mask = (df.index >= start_date) & (df.index <= end_date)
        eval_dates = df.index[mask]
        date_locs = df.index.get_indexer(eval_dates)

        for current_date, loc in zip(eval_dates, date_locs):
            if loc + 1 < self.config.min_bars:
                continue
                
            slice_df = df.iloc[:loc + 1]

            scan_date = current_date.strftime("%Y-%m-%d")
            scan_time = "15:30:00"
            
            try:
                res = self._evaluate_symbol(sym, slice_df, scan_date, scan_time)
                if res is not None and res.passed:
                    res_list.append(res)
            except Exception as e:
                logger.error("Historical error for %s on %s: %s", sym, scan_date, e)
                err_count += 1
                
        return res_list, err_count



    def process_tick(self, tick: TickData, df_history: pd.DataFrame) -> ScreenResult | None:
        """
        Fast path for live data stream.
        Appends the tick to history and evaluates.
        """
        # Validate tick
        is_valid, err = self.validator.validate_tick(tick.symbol, tick)
        if not is_valid:
            logger.debug("[%s] Tick validation failed: %s", tick.symbol, err)
            return None

        # Pre-filter tick
        passed_filter, filter_err = self.pre_filter.apply_tick(tick.symbol, tick)
        if not passed_filter:
            logger.debug("[%s] Tick pre-filter failed: %s", tick.symbol, filter_err)
            return None

        # Convert tick to row and append to history
        row = tick.to_ohlcv_row()
        tick_df = pd.DataFrame([row]).set_index("timestamp")
        combined_df = pd.concat([df_history, tick_df])
        
        scan_date = tick.timestamp.strftime("%Y-%m-%d")
        scan_time = tick.timestamp.strftime("%H:%M:%S")

        res = self._evaluate_symbol(tick.symbol, combined_df, scan_date, scan_time)
        if res:
            res.ltp = tick.ltp
            res.bid = tick.bid_prices[0] if tick.bid_prices else None
            res.ask = tick.ask_prices[0] if tick.ask_prices else None
            res.depth = {
                "bids": tick.bid_prices,
                "asks": tick.ask_prices
            }
        return res

```

---

## File: `./filters.py`

```python
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from screener.base import TickData

class DataValidator:
    """Validates data structures before processing."""

    @staticmethod
    def validate(symbol: str, df: pd.DataFrame) -> tuple[bool, str]:
        if df is None or df.empty:
            return False, f"{symbol}: DataFrame is empty"
        
        required_cols = ["open", "high", "low", "close", "volume"]
        missing_cols = [c for c in required_cols if c not in df.columns]
        if missing_cols:
            return False, f"{symbol}: Missing required columns {missing_cols}"
            
        if not isinstance(df.index, pd.DatetimeIndex):
            return False, f"{symbol}: Index must be a DatetimeIndex"
            
        # Check if any row has all NaNs in the required columns
        if df[required_cols].isna().all(axis=1).any():
            return False, f"{symbol}: Contains rows with all NaN values in OHLCV columns"
            
        return True, ""

    @staticmethod
    def validate_tick(symbol: str, tick: TickData) -> tuple[bool, str]:
        if tick is None:
            return False, f"{symbol}: TickData is None"
        if tick.symbol != symbol:
            return False, f"{symbol}: TickData symbol {tick.symbol} does not match {symbol}"
        if not tick.validate():
            return False, f"{symbol}: TickData is invalid"
        return True, ""


@dataclass
class PreFilter:
    """Filters out instruments before running heavy indicator logic."""
    min_bars: int = 50
    min_price: float = 0.0
    max_price: float = 0.0
    min_volume: float = 0.0
    min_atr_pct: float = 0.0
    required_columns: list[str] = field(default_factory=lambda: ["open", "high", "low", "close", "volume"])

    def apply(self, symbol: str, df: pd.DataFrame) -> tuple[bool, str]:
        missing_cols = [c for c in self.required_columns if c not in df.columns]
        if missing_cols:
            return False, f"Missing required columns {missing_cols}"
            
        if len(df) < self.min_bars:
            return False, f"Insufficient bars: {len(df)} < {self.min_bars}"
            
        last_row = df.iloc[-1]
        close_price = float(last_row["close"])
        volume = float(last_row["volume"])
        
        if self.min_price > 0 and close_price < self.min_price:
            return False, f"Price below minimum: {close_price} < {self.min_price}"
            
        if self.max_price > 0 and close_price > self.max_price:
            return False, f"Price above maximum: {close_price} > {self.max_price}"
            
        if self.min_volume > 0 and volume < self.min_volume:
            return False, f"Volume below minimum: {volume} < {self.min_volume}"
            
        if self.min_atr_pct > 0:
            if len(df) >= 14:
                high = df["high"].to_numpy()
                low = df["low"].to_numpy()
                close = df["close"].to_numpy()
                
                prev_close = np.roll(close, 1)
                prev_close[0] = close[0]
                
                tr1 = high - low
                tr2 = np.abs(high - prev_close)
                tr3 = np.abs(low - prev_close)
                tr = np.maximum(tr1, np.maximum(tr2, tr3))
                
                atr = pd.Series(tr).rolling(window=14).mean().iloc[-1]
                
                if pd.notna(atr) and close_price > 0:
                    atr_pct = (atr / close_price) * 100
                    if atr_pct < self.min_atr_pct:
                        return False, f"ATR % below minimum: {atr_pct:.2f} < {self.min_atr_pct}"
            else:
                return False, f"Not enough bars for ATR calculation: {len(df)} < 14"
                
        return True, ""

    def apply_tick(self, symbol: str, tick: TickData) -> tuple[bool, str]:
        if self.min_price > 0 and tick.ltp < self.min_price:
            return False, f"Tick price below minimum: {tick.ltp} < {self.min_price}"
            
        if self.max_price > 0 and tick.ltp > self.max_price:
            return False, f"Tick price above maximum: {tick.ltp} > {self.max_price}"
            
        if self.min_volume > 0 and tick.volume < self.min_volume:
            return False, f"Tick volume below minimum: {tick.volume} < {self.min_volume}"
            
        return True, ""

```

---

## File: `./output.py`

```python
"""
screener/output.py
------------------
Output formatting and history management for screener results.
"""

from __future__ import annotations

import json
from datetime import datetime
from core.clock import clock_now
from pathlib import Path
from typing import Union

import pandas as pd

from screener.base import ScanSummary, ExportFormat


class OutputFormatter:
    """Formats scan results to CSV or JSON."""
    
    def format_csv(self, summary: ScanSummary, filepath: Union[str, Path]) -> Path:
        """Saves summary.results as CSV using .to_row()."""
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        rows = [r.to_row() for r in summary.results]
        if not rows:
            pd.DataFrame().to_csv(filepath, index=False)
            return filepath

        df = pd.DataFrame(rows)
        df.to_csv(filepath, index=False)
        return filepath

    def format_json(self, summary: ScanSummary, filepath: Union[str, Path]) -> Path:
        """Saves summary.to_dict() as JSON."""
        filepath = Path(filepath)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(summary.to_dict(), f, indent=4)
        return filepath

    def to_console(self, summary: ScanSummary) -> None:
        """Prints a tabular summary of the scan results to the console."""
        print(f"\n--- Scan Summary: {summary.scan_name} ---")
        print(f"Mode: {summary.mode.value} | Date: {summary.scan_date}")
        print(f"Scanned: {summary.symbols_scanned} | Passed: {summary.symbols_passed} | Failed: {summary.symbols_failed}")
        print("-" * 50)
        for r in summary.results:
            print(f"[{r.symbol}] Score: {r.score:.2f} | Close: {r.close:.2f} | Dir: {r.signal_direction.name}")

    def historical_to_csv(self, summary: ScanSummary, filepath: Union[str, Path]) -> Path:
        """Saves historical scan results as CSV."""
        return self.format_csv(summary, filepath)


class ScreenerHistory:
    """Manages history of screener runs."""
    
    def __init__(self, history_dir: Union[str, Path]):
        self.history_dir = Path(history_dir)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.formatter = OutputFormatter()
        
    def save_scan(self, summary: ScanSummary, export_format: ExportFormat = ExportFormat.BOTH) -> list[Path]:
        """Saves results based on export_format."""
        timestamp = clock_now().strftime("%Y%m%d_%H%M%S")
        name = summary.scan_name.replace(" ", "_")
        base_name = f"scan_{name}_{timestamp}"
        
        paths = []
        if export_format in (ExportFormat.CSV, ExportFormat.BOTH):
            csv_path = self.history_dir / f"{base_name}.csv"
            paths.append(self.formatter.format_csv(summary, csv_path))
            
        if export_format in (ExportFormat.JSON, ExportFormat.BOTH):
            json_path = self.history_dir / f"{base_name}.json"
            paths.append(self.formatter.format_json(summary, json_path))
            
        return paths

```

---

## File: `./scoring.py`

```python
from enum import Enum
from typing import Any

from screener.base import RankBy, ScreenResult, RuleResult


class ScoreMode(Enum):
    HIT_COUNT = "HIT_COUNT"
    WEIGHTED = "WEIGHTED"


class Scorer:
    def __init__(self, mode: ScoreMode = ScoreMode.HIT_COUNT):
        self.mode = mode

    def score(self, rule_results: dict[str, RuleResult], weights: dict[str, float] | None = None) -> float:
        if not rule_results:
            return 0.0

        if self.mode == ScoreMode.HIT_COUNT:
            passed = sum(1 for res in rule_results.values() if res.passed)
            return float(passed) / len(rule_results)
        
        elif self.mode == ScoreMode.WEIGHTED:
            total_weight = 0.0
            earned_weight = 0.0
            
            for rule_name, res in rule_results.items():
                w = weights.get(rule_name, res.weight) if weights else res.weight
                total_weight += w
                if res.passed:
                    earned_weight += w
                    
            if total_weight == 0.0:
                return 0.0
                
            return float(earned_weight) / total_weight

        return 0.0

    def rank(self, results: list[ScreenResult], rank_by: RankBy, ascending: bool = False) -> list[ScreenResult]:
        def extract_value(res: ScreenResult, keys: list[str]) -> float | None:
            for key in keys:
                if key in res.indicator_values:
                    return res.indicator_values[key]
            for r in res.rule_details.values():
                if r.details:
                    for key in keys:
                        if key in r.details:
                            return r.details[key]
            return None

        def get_sort_key(res: ScreenResult) -> tuple[int, float]:
            val: float | None = None
            
            if rank_by == RankBy.SCORE:
                val = res.score
            elif rank_by == RankBy.CLOSE:
                val = res.close
            elif rank_by == RankBy.VOLUME:
                val = res.volume
            elif rank_by == RankBy.ATR_PCT:
                val = res.atr_pct
            elif rank_by == RankBy.RS_SCORE:
                val = extract_value(res, ["rs_score", "RS_SCORE"])
                if val is None:
                    val = res.score
            elif rank_by == RankBy.SIGNAL_STRENGTH:
                val = extract_value(res, ["signal_strength", "SIGNAL_STRENGTH"])
                if val is None:
                    val = res.score

            if val is None:
                return (1, 0.0) if ascending else (0, 0.0)
            return (0, float(val)) if ascending else (1, float(val))

        return sorted(results, key=get_sort_key, reverse=not ascending)

```

---

## File: `./universe.py`

```python
"""
screener/universe.py
--------------------
Manages the universe of symbols to be screened.
"""

import logging
from dataclasses import dataclass, field
import pandas as pd
from typing import Dict, List, Self

logger = logging.getLogger(__name__)

@dataclass
class Universe:
    name: str
    symbols: List[str]
    description: str = ""
    
    def __post_init__(self):
        if not self.symbols:
            logger.warning("Universe '%s' initialized with empty symbols list.", self.name)
            # We don't raise an error here to allow empty universes, but it's warned.
            

    def validate(self, data_dict: Dict[str, pd.DataFrame]) -> List[str]:
        """Returns list of symbols that are in `symbols` but missing from `data_dict`."""
        return [sym for sym in self.symbols if sym not in data_dict]
        
    def filter(self, data_dict: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """Returns only symbols present in both universe and data_dict."""
        return {sym: df for sym, df in data_dict.items() if sym in self.symbols}
        
    @classmethod
    def custom(cls, symbols: List[str]) -> Self:
        return cls(name="Custom", symbols=symbols)
        
    @classmethod
    def from_list(cls, name: str, symbols: List[str]) -> Self:
        return cls(name=name, symbols=symbols)

```

---

## File: `./example_screener/minervini.py`

```python
import logging
import pandas as pd
import sys

from screener.base import ScreenerConfig, ScreenMode, RankBy, RuleResult, ExportFormat
from screener.engine import ScreenerEngine
from screener.rules.base import ScreenRule
from screener.rules.vcp import VCPRule
from data.universe import UniverseManager
from data.manager import DataManager
from data.providers.yfinance_provider import YFinanceProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class MinerviniRule(ScreenRule):
    name = "MinerviniTrendTemplate"
    description = "Mark Minervini's Trend Template"
    min_bars_required = 260
    weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        close = df["close"]
        low = df["low"]
        high = df["high"]
        
        # Calculate moving averages
        sma50 = close.rolling(window=50).mean().iloc[-1]
        sma150 = close.rolling(window=150).mean().iloc[-1]
        sma200 = close.rolling(window=200).mean().iloc[-1]
        current_price = close.iloc[-1]
        
        # 52-week High/Low (approx 252 trading days)
        low_52wk = low.tail(252).min()
        high_52wk = high.tail(252).max()
        
        # Minervini's Criteria
        cond1 = current_price > sma150 and current_price > sma200
        cond2 = sma150 > sma200
        
        # Trend of 200 SMA (proxy: compare with 1 month ago)
        sma200_1m_ago = close.rolling(window=200).mean().iloc[-20] if len(close) >= 220 else sma200
        cond3 = sma200 > sma200_1m_ago
        
        cond4 = sma50 > sma150 and sma50 > sma200
        cond5 = current_price > sma50
        cond6 = current_price >= (low_52wk * 1.30)
        cond7 = current_price >= (high_52wk * 0.75)
        
        passed = cond1 and cond2 and cond3 and cond4 and cond5 and cond6 and cond7
        
        details = {
            "sma50": float(sma50), "sma150": float(sma150), "sma200": float(sma200),
            "low_52wk": float(low_52wk), "high_52wk": float(high_52wk)
        }
        
        return RuleResult(self.name, bool(passed), float(current_price), None, details, self.weight)

def main():
    um = UniverseManager()
    symbols = um.get_universe("NIFTY_500")
    if not symbols:
        logger.warning("NIFTY_500 universe not found. Ensure you run universe_updater.py first.")
        sys.exit(1)

    # Use YFinance as provider for reliable historical EOD data
    mgr = DataManager(provider=YFinanceProvider())
    logger.info(f"Fetching 2 years of data for {len(symbols)} symbols from NSE500 (cache-first)...")
    
    data_dict = mgr.fetch_bulk(symbols, interval="daily", period="2years", use_cache=True)
    
    # Filter out symbols without enough history
    data_dict = {sym: df for sym, df in data_dict.items() if not df.empty and len(df) >= 260}
    
    if not data_dict:
        logger.error("No valid data retrieved.")
        sys.exit(1)

    config = ScreenerConfig(
        scan_name="Minervini_NSE500",
        mode=ScreenMode.EOD,
        symbols=list(data_dict.keys()),
        min_bars=260,
        n_workers=8,
        rank_by=RankBy.CLOSE,
        rank_ascending=False,
        export_format=ExportFormat.CSV
    )

    vcp_mode = "setup" # Change to "breakout" for breakout mode
    composite_rule = MinerviniRule() & VCPRule(pivot_mode=vcp_mode)
    engine = ScreenerEngine(config=config, rule=composite_rule)
    logger.info(f"Running Minervini Screener with VCP ({vcp_mode.upper()} mode)...")
    summary = engine.run_eod(data_dict)
    
    logger.info(f"Scan complete: {summary.symbols_passed} passed out of {summary.symbols_scanned}.")
    
    print("\n" + "="*50)
    print(f"  Mark Minervini SEPA (Trend + VCP) - {vcp_mode.upper()}")
    print("="*50)
    
    count = 0
    for res in summary.results:
        if res.passed:
            count += 1
            # res is a ScreenResult. We need to access the composite rule's result
            vcp_res = res.rule_details.get("VolatilityContractionPattern")
            footprint = ""
            if isinstance(vcp_res, dict):
                footprint = vcp_res.get('details', {}).get('footprint', '')
            elif vcp_res:
                footprint = vcp_res.details.get('footprint', '')
            print(f"{count:>2}. {res.symbol:<15} Close: {res.close:<8.2f} VCP: {footprint}")
            
    if count == 0:
        print("No stocks passed the criteria today.")

if __name__ == "__main__":
    main()

```

---

## File: `./rules/__init__.py`

```python
"""
screener/rules/__init__.py
--------------------------
Rule exports for screener.
"""

from screener.rules.base import ScreenRule
from screener.rules.composite import AndRule, OrRule, NotRule, AtLeastNRule
from screener.rules.technical import (
    RSIRule, MACDRule, EMARule, GoldenCrossRule, DeathCrossRule,
    SupertrendRule, BollingerSqueezeRule, RelativeStrengthRule,
    ADXRule, VWAPRule
)
from screener.rules.price import (
    PriceBreakoutRule, NearSupportRule, NearResistanceRule,
    PriceRangeRule, HigherHighRule, LowerLowRule
)
from screener.rules.volume import (
    VolumeBreakoutRule, VolumeDeclineRule, VolumeRatioRule,
    AccumulationRule, DistributionRule
)
from screener.rules.pattern import (
    CandlePatternRule, ChartPatternRule, TrendStructureRule
)
from screener.rules.strategy import StrategyRule, StrategyConfluenceRule
from screener.rules.serializer import RuleSerializer, build_rule_from_dict

__all__ = [
    "ScreenRule",
    "AndRule", "OrRule", "NotRule", "AtLeastNRule",
    "RSIRule", "MACDRule", "EMARule", "GoldenCrossRule", "DeathCrossRule",
    "SupertrendRule", "BollingerSqueezeRule", "RelativeStrengthRule",
    "ADXRule", "VWAPRule",
    "PriceBreakoutRule", "NearSupportRule", "NearResistanceRule",
    "PriceRangeRule", "HigherHighRule", "LowerLowRule",
    "VolumeBreakoutRule", "VolumeDeclineRule", "VolumeRatioRule",
    "AccumulationRule", "DistributionRule",
    "CandlePatternRule", "ChartPatternRule", "TrendStructureRule",
    "StrategyRule", "StrategyConfluenceRule",
    "RuleSerializer", "build_rule_from_dict"
]

```

---

## File: `./rules/base.py`

```python
"""
screener/rules/base.py
----------------------
Abstract Base Class for all screening rules.
"""

import logging
from abc import ABC, abstractmethod

import pandas as pd

from screener.base import RuleResult

logger = logging.getLogger(__name__)


class ScreenRule(ABC):
    """
    Abstract Base Class for a screener rule.
    """
    name: str = "BaseRule"
    description: str = "Base rule"
    min_bars_required: int = 1
    weight: float = 1.0

    @abstractmethod
    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        """
        Evaluate the rule against an OHLCV DataFrame.
        This method must be implemented by subclasses.
        """
        ...

    def evaluate_safe(self, df: pd.DataFrame) -> RuleResult:
        """
        Wraps evaluate() in try/except and enforces min_bars_required.
        Returns a failed RuleResult on error or insufficient data.
        """
        if len(df) < self.min_bars_required:
            logger.debug("Rule %s failed: insufficient bars (needs %s, got %s)", self.name, self.min_bars_required, len(df))
            return RuleResult(
                rule_name=self.name,
                passed=False,
                value=None,
                threshold=None,
                details={"error": "insufficient_data"},
                weight=self.weight
            )
        try:
            return self.evaluate(df)
        except Exception as e:
            logger.exception("Rule %s evaluation error: %s", self.name, e)
            return RuleResult(
                rule_name=self.name,
                passed=False,
                value=None,
                threshold=None,
                details={"error": str(e)},
                weight=self.weight
            )

    def to_dict(self) -> dict:
        """
        Serializes rule name + params for JSON storage.
        """
        return {"type": self.__class__.__name__}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        """
        Deserializes from JSON.
        """
        raise NotImplementedError("from_dict must be implemented by serializer or concrete class")

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"

    def __and__(self, other: "ScreenRule") -> "ScreenRule":
        from screener.rules.composite import AndRule
        return AndRule([self, other])

    def __or__(self, other: "ScreenRule") -> "ScreenRule":
        from screener.rules.composite import OrRule
        return OrRule([self, other])

    def __invert__(self) -> "ScreenRule":
        from screener.rules.composite import NotRule
        return NotRule(self)

```

---

## File: `./rules/composite.py`

```python
"""
screener/rules/composite.py
---------------------------
Unlimited nesting logic (AND, OR, NOT, AtLeastN).
"""

from typing import Any

import pandas as pd

from screener.base import RuleResult
from screener.rules.base import ScreenRule


class AndRule(ScreenRule):
    def __init__(self, rules: list[ScreenRule]):
        self.rules = rules
        self.name = "AND(" + ", ".join(r.name for r in rules) + ")"
        self.description = "Logical AND composition"
        self.min_bars_required = max((r.min_bars_required for r in rules), default=1)
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        details = {}
        passed = True
        for rule in self.rules:
            res = rule.evaluate_safe(df)
            details[rule.name] = res
            if not res.passed:
                passed = False
                break  # Short-circuit
        return RuleResult(
            rule_name=self.name,
            passed=passed,
            value=None,
            threshold=None,
            details=details,
            weight=self.weight
        )

    def to_dict(self) -> dict:
        return {
            "type": "AndRule",
            "rules": [r.to_dict() for r in self.rules]
        }

    def __repr__(self) -> str:
        return f"AndRule({self.rules})"


class OrRule(ScreenRule):
    def __init__(self, rules: list[ScreenRule]):
        self.rules = rules
        self.name = "OR(" + ", ".join(r.name for r in rules) + ")"
        self.description = "Logical OR composition"
        self.min_bars_required = max((r.min_bars_required for r in rules), default=1)
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        details = {}
        passed = False
        for rule in self.rules:
            res = rule.evaluate_safe(df)
            details[rule.name] = res
            if res.passed:
                passed = True
                break  # Short-circuit
        return RuleResult(
            rule_name=self.name,
            passed=passed,
            value=None,
            threshold=None,
            details=details,
            weight=self.weight
        )

    def to_dict(self) -> dict:
        return {
            "type": "OrRule",
            "rules": [r.to_dict() for r in self.rules]
        }

    def __repr__(self) -> str:
        return f"OrRule({self.rules})"


class NotRule(ScreenRule):
    def __init__(self, rule: ScreenRule):
        self.rule = rule
        self.name = f"NOT({rule.name})"
        self.description = "Logical NOT composition"
        self.min_bars_required = rule.min_bars_required
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        res = self.rule.evaluate_safe(df)
        return RuleResult(
            rule_name=self.name,
            passed=not res.passed,
            value=None,
            threshold=None,
            details={self.rule.name: res},
            weight=self.weight
        )

    def to_dict(self) -> dict:
        return {
            "type": "NotRule",
            "rule": self.rule.to_dict()
        }

    def __repr__(self) -> str:
        return f"NotRule({self.rule})"


class AtLeastNRule(ScreenRule):
    def __init__(self, rules: list[ScreenRule], n: int):
        self.rules = rules
        self.n = n
        self.name = f"AtLeast{n}(...)"
        self.description = f"At least {n} rules must pass"
        self.min_bars_required = max((r.min_bars_required for r in rules), default=1)
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        details = {}
        passed_count = 0
        for rule in self.rules:
            res = rule.evaluate_safe(df)
            details[rule.name] = res
            if res.passed:
                passed_count += 1
        
        passed = passed_count >= self.n
        score = passed_count / max(1, len(self.rules))
        return RuleResult(
            rule_name=self.name,
            passed=passed,
            value=score,
            threshold=self.n,
            details=details,
            weight=self.weight
        )

    def to_dict(self) -> dict:
        return {
            "type": "AtLeastNRule",
            "n": self.n,
            "rules": [r.to_dict() for r in self.rules]
        }

    def __repr__(self) -> str:
        return f"AtLeastNRule(n={self.n}, rules={self.rules})"

```

---

## File: `./rules/pattern.py`

```python
"""
screener/rules/pattern.py
-------------------------
Pattern recognition rules for the screener.
"""

import pandas as pd

from screener.base import RuleResult, SignalDirection
from screener.rules.base import ScreenRule
from indicators.patterns.candlestick import __all__ as candlestick_patterns


class CandlePatternRule(ScreenRule):
    def __init__(self, pattern_name: str, direction: SignalDirection = SignalDirection.BULLISH):
        self.pattern_name = pattern_name
        self.direction = direction
        self.name = f"Candle({pattern_name}) {direction.name}"
        self.description = f"Candlestick pattern: {pattern_name}"
        self.min_bars_required = 10
        self.weight = 1.0
        
        # Verify pattern exists
        if pattern_name not in candlestick_patterns and pattern_name != "scan_all_candlestick":
            raise ValueError(f"Pattern {pattern_name} not found in indicators.patterns.candlestick")

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        import importlib
        try:
            candlestick = importlib.import_module("indicators.patterns.candlestick")
            func = getattr(candlestick, self.pattern_name)
            # candlestick functions take (open, high, low, close)
            res = func(df["open"], df["high"], df["low"], df["close"])
            val = res.iloc[-1]
            
            if self.direction == SignalDirection.BULLISH:
                passed = val == 1
            elif self.direction == SignalDirection.BEARISH:
                passed = val == -1
            elif self.direction == SignalDirection.ANY:
                passed = val != 0
            else:
                passed = False
                
        except Exception as e:
            passed = False
            val = None

        return RuleResult(self.name, bool(passed), float(val) if val is not None else None, None, {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "CandlePatternRule", "pattern_name": self.pattern_name, "direction": self.direction.name}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        direction_str = data.get("direction", "BULLISH")
        direction = getattr(SignalDirection, direction_str, SignalDirection.BULLISH)
        return cls(pattern_name=data.get("pattern_name", "doji"), direction=direction)


class ChartPatternRule(ScreenRule):
    def __init__(self, pattern_name: str):
        self.pattern_name = pattern_name
        self.name = f"ChartPattern({pattern_name})"
        self.description = f"Chart pattern: {pattern_name}"
        self.min_bars_required = 50
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        import importlib
        try:
            chart_patterns = importlib.import_module("indicators.patterns.chart_patterns")
            func = getattr(chart_patterns, self.pattern_name)
            res = func(df)
            val = res.iloc[-1]
            passed = val == 1
        except Exception as e:
            passed = False
            val = None

        return RuleResult(self.name, bool(passed), float(val) if val is not None else None, None, {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "ChartPatternRule", "pattern_name": self.pattern_name}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(pattern_name=data.get("pattern_name", "double_bottom"))


class TrendStructureRule(ScreenRule):
    def __init__(self, direction: str = "uptrend", lookback: int = 20):
        self.direction = direction
        self.lookback = lookback
        self.name = f"Trend({direction})"
        self.description = f"Trend structure: {direction}"
        self.min_bars_required = lookback + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        from indicators.patterns.dow_patterns import trend_structure
        ts = trend_structure(df["high"], df["low"])
        val = ts[self.direction].iloc[-1]
        passed = bool(val)
        
        return RuleResult(self.name, passed, 1.0 if passed else 0.0, None, {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "TrendStructureRule", "direction": self.direction, "lookback": self.lookback}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(direction=data.get("direction", "uptrend"), lookback=data.get("lookback", 20))

```

---

## File: `./rules/price.py`

```python
"""
screener/rules/price.py
-----------------------
Price Action Rules for the screener.
"""

import pandas as pd

from screener.base import RuleResult
from screener.rules.base import ScreenRule


class PriceBreakoutRule(ScreenRule):
    def __init__(self, lookback: int = 20, direction: str = "above"):
        self.lookback = lookback
        self.direction = direction
        self.name = f"Breakout({lookback}) {direction}"
        self.description = f"Price breaks {lookback}-bar high/low"
        self.min_bars_required = lookback + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        close = df["close"].iloc[-1]
        prev_close = df["close"].iloc[-2]
        
        if self.direction == "above":
            # high over previous N bars (not including current)
            n_high = df["high"].iloc[-self.lookback-1:-1].max()
            passed = prev_close <= n_high and close > n_high
            threshold = float(n_high)
        elif self.direction == "below":
            n_low = df["low"].iloc[-self.lookback-1:-1].min()
            passed = prev_close >= n_low and close < n_low
            threshold = float(n_low)
        else:
            passed = False
            threshold = None

        return RuleResult(self.name, bool(passed), float(close), threshold, {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "PriceBreakoutRule", "lookback": self.lookback, "direction": self.direction}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(lookback=data.get("lookback", 20), direction=data.get("direction", "above"))


class NearSupportRule(ScreenRule):
    def __init__(self, sr_levels: list = None, tolerance_pct: float = 0.02):
        self.sr_levels = sr_levels or []
        self.tolerance_pct = tolerance_pct
        self.name = f"NearSupport({tolerance_pct*100}%)"
        self.description = "Price within tolerance of a support level"
        self.min_bars_required = 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        close = df["close"].iloc[-1]
        passed = False
        nearest = None

        if self.sr_levels:
            supports = [l for l in self.sr_levels if l.price <= close * (1 + self.tolerance_pct)]
            if supports:
                nearest = max(supports, key=lambda x: x.price)
                if close <= nearest.price * (1 + self.tolerance_pct) and close >= nearest.price * (1 - self.tolerance_pct):
                    passed = True
                else:
                    passed = False

        return RuleResult(self.name, bool(passed), float(close), float(nearest.price) if nearest else None, {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "NearSupportRule", "tolerance_pct": self.tolerance_pct}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(sr_levels=[], tolerance_pct=data.get("tolerance_pct", 0.02))


class NearResistanceRule(ScreenRule):
    def __init__(self, sr_levels: list = None, tolerance_pct: float = 0.02):
        self.sr_levels = sr_levels or []
        self.tolerance_pct = tolerance_pct
        self.name = f"NearResistance({tolerance_pct*100}%)"
        self.description = "Price within tolerance of a resistance level"
        self.min_bars_required = 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        close = df["close"].iloc[-1]
        passed = False
        nearest = None

        if self.sr_levels:
            resistances = [l for l in self.sr_levels if l.price >= close * (1 - self.tolerance_pct)]
            if resistances:
                nearest = min(resistances, key=lambda x: x.price)
                if close >= nearest.price * (1 - self.tolerance_pct) and close <= nearest.price * (1 + self.tolerance_pct):
                    passed = True
                else:
                    passed = False

        return RuleResult(self.name, bool(passed), float(close), float(nearest.price) if nearest else None, {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "NearResistanceRule", "tolerance_pct": self.tolerance_pct}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(sr_levels=[], tolerance_pct=data.get("tolerance_pct", 0.02))


class PriceRangeRule(ScreenRule):
    def __init__(self, min_price: float = 0.0, max_price: float = 0.0):
        self.min_price = min_price
        self.max_price = max_price
        self.name = f"PriceRange({min_price}-{max_price})"
        self.description = "Price band filter"
        self.min_bars_required = 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        close = df["close"].iloc[-1]
        passed = True
        if self.min_price > 0 and close < self.min_price:
            passed = False
        if self.max_price > 0 and close > self.max_price:
            passed = False
        return RuleResult(self.name, bool(passed), float(close), None, {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "PriceRangeRule", "min_price": self.min_price, "max_price": self.max_price}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(min_price=data.get("min_price", 0.0), max_price=data.get("max_price", 0.0))


class HigherHighRule(ScreenRule):
    def __init__(self, lookback: int = 20, order: int = 5):
        self.lookback = lookback
        self.order = order
        self.name = f"HigherHigh({lookback},{order})"
        self.description = "Price making higher highs"
        self.min_bars_required = lookback + order * 2 + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        from indicators.patterns.dow_patterns import find_swings
        # Get recent swings
        swings = find_swings(df["high"], df["low"], window=self.order)
        swing_highs = swings[swings["swing_high"]]["swing_high_price"].dropna()
        
        if len(swing_highs) >= 2:
            last_high = swing_highs.iloc[-1]
            prev_high = swing_highs.iloc[-2]
            passed = last_high > prev_high
        else:
            passed = False
            last_high = None

        return RuleResult(self.name, bool(passed), float(last_high) if last_high else None, None, {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "HigherHighRule", "lookback": self.lookback, "order": self.order}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(lookback=data.get("lookback", 20), order=data.get("order", 5))


class LowerLowRule(ScreenRule):
    def __init__(self, lookback: int = 20, order: int = 5):
        self.lookback = lookback
        self.order = order
        self.name = f"LowerLow({lookback},{order})"
        self.description = "Price making lower lows"
        self.min_bars_required = lookback + order * 2 + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        from indicators.patterns.dow_patterns import find_swings
        swings = find_swings(df["high"], df["low"], window=self.order)
        swing_lows = swings[swings["swing_low"]]["swing_low_price"].dropna()
        
        if len(swing_lows) >= 2:
            last_low = swing_lows.iloc[-1]
            prev_low = swing_lows.iloc[-2]
            passed = last_low < prev_low
        else:
            passed = False
            last_low = None

        return RuleResult(self.name, passed, float(last_low) if last_low else None, None, {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "LowerLowRule", "lookback": self.lookback, "order": self.order}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(lookback=data.get("lookback", 20), order=data.get("order", 5))


class HorizontalZoneRule(ScreenRule):
    def __init__(self, order: int = 5, tolerance_pct: float = 0.005, min_touches: int = 3, signal_type: str = "any"):
        self.order = order
        self.tolerance_pct = tolerance_pct
        self.min_touches = min_touches
        self.signal_type = signal_type.lower()
        self.name = f"HorizZone({min_touches}T,{tolerance_pct*100}%)"
        self.description = f"Price near horizontal zone with >={min_touches} reversals"
        self.min_bars_required = 100
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        from scipy.signal import find_peaks
        import numpy as np
        
        close_series = df["close"].values
        high_series = df["high"].values
        low_series = df["low"].values
        current_close = float(close_series[-1])
        
        pivots = []
        if self.signal_type in ("bullish", "any"):
            # Swing lows
            peaks, _ = find_peaks(-low_series, distance=self.order)
            for idx in peaks:
                pivots.append(("support", float(low_series[idx]), idx))
                
        if self.signal_type in ("bearish", "any"):
            # Swing highs
            peaks, _ = find_peaks(high_series, distance=self.order)
            for idx in peaks:
                pivots.append(("resistance", float(high_series[idx]), idx))
                
        # Group pivots into clusters (zones)
        clusters = [] # list of dicts: {"type": "support", "prices": [], "indices": []}
        
        for p_type, p_val, idx in sorted(pivots, key=lambda x: x[1]):
            placed = False
            for c in clusters:
                if c["type"] == p_type:
                    c_min = min(c["prices"])
                    c_max = max(c["prices"])
                    # Check if adding this price keeps cluster width within tolerance
                    new_min = min(c_min, p_val)
                    new_max = max(c_max, p_val)
                    if (new_max - new_min) / new_min <= self.tolerance_pct:
                        c["prices"].append(p_val)
                        c["indices"].append(idx)
                        placed = True
                        break
            if not placed:
                clusters.append({"type": p_type, "prices": [p_val], "indices": [idx]})
                
        passed = False
        details = {}
        
        for i, c in enumerate(clusters):
            zone_min = min(c["prices"])
            zone_max = max(c["prices"])
            zone_type = c["type"]
            first_idx = min(c["indices"])
            
            # Hit Counter: Count reversals out of this zone
            touches = 0
            in_zone = False
            
            # Start counting after the first pivot establishes the potential zone
            for k in range(first_idx + 1, len(df)):
                k_low = low_series[k]
                k_high = high_series[k]
                k_close = close_series[k]
                
                if zone_type == "support":
                    if k_low <= zone_max:
                        in_zone = True
                    if in_zone and k_close > zone_max:
                        touches += 1
                        in_zone = False
                else: # resistance
                    if k_high >= zone_min:
                        in_zone = True
                    if in_zone and k_close < zone_min:
                        touches += 1
                        in_zone = False
                        
            if touches >= self.min_touches:
                # Valid zone. Check if current price is near it
                is_near = False
                if zone_type == "support" and (current_close - zone_max) / zone_max <= self.tolerance_pct and current_close >= zone_min:
                    is_near = True
                elif zone_type == "resistance" and (zone_min - current_close) / current_close <= self.tolerance_pct and current_close <= zone_max:
                    is_near = True
                elif current_close >= zone_min and current_close <= zone_max:
                    is_near = True
                    
                if is_near:
                    passed = True
                    details[f"zone_{i}"] = {
                        "type": zone_type, 
                        "zone_min": zone_min, 
                        "zone_max": zone_max, 
                        "touches": touches
                    }
                    
        return RuleResult(self.name, passed, current_close, None, details, self.weight)

    def to_dict(self) -> dict:
        return {
            "type": "HorizontalZoneRule", 
            "order": self.order, 
            "tolerance_pct": self.tolerance_pct,
            "min_touches": self.min_touches,
            "signal_type": self.signal_type
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(
            order=data.get("order", 5),
            tolerance_pct=data.get("tolerance_pct", 0.005),
            min_touches=data.get("min_touches", 3),
            signal_type=data.get("signal_type", "any")
        )


class TrendlineRule(ScreenRule):
    def __init__(self, lookback: int = 200, order: int = 5, thickness_multiplier: float = 1.0, min_touches: int = 3, signal_type: str = "any"):
        self.lookback = lookback
        self.order = order
        self.thickness_multiplier = thickness_multiplier
        self.min_touches = min_touches
        self.signal_type = signal_type.lower()
        self.name = f"Trendline({min_touches}T)"
        self.description = f"Price near inclined trendline with >={min_touches} reversals"
        self.min_bars_required = self.lookback
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        from scipy.signal import find_peaks
        from scipy.stats import linregress
        import numpy as np
        
        df_slice = df.iloc[-self.lookback:] if len(df) > self.lookback else df
        close_series = df_slice["close"].values
        high_series = df_slice["high"].values
        low_series = df_slice["low"].values
        current_close = float(close_series[-1])
        current_idx = len(df_slice) - 1
        
        passed = False
        details = {}
        
        def find_valid_trendlines(p_type):
            if p_type == "support":
                peaks, _ = find_peaks(-low_series, distance=self.order)
                prices = low_series
            else:
                peaks, _ = find_peaks(high_series, distance=self.order)
                prices = high_series
                
            valid_lines = []
            if len(peaks) < 2:
                return valid_lines
                
            for i in range(len(peaks)):
                for j in range(i + 1, len(peaks)):
                    x_idx = np.array([peaks[i], peaks[j]])
                    y_val = np.array([prices[peaks[i]], prices[peaks[j]]])
                    
                    if x_idx[0] == x_idx[1]: continue
                    
                    res = linregress(x_idx, y_val)
                    m, c = res.slope, res.intercept
                    
                    # Calculate Residual Standard Error (RSE)
                    segment_x = np.arange(peaks[i], peaks[j] + 1)
                    segment_y = prices[peaks[i]:peaks[j] + 1]
                    y_pred = m * segment_x + c
                    
                    residuals = segment_y - y_pred
                    if len(segment_x) > 2:
                        rse = np.sqrt(np.sum(residuals**2) / (len(segment_x) - 2))
                    else:
                        rse = np.std(residuals) if len(residuals) > 0 else 0
                        
                    thickness = self.thickness_multiplier * rse
                    if thickness == 0:
                        thickness = prices[peaks[i]] * 0.005
                        
                    # Hit Counter (Reversals)
                    touches = 0
                    in_zone = False
                    
                    # Scan from first pivot to current bar
                    for k in range(peaks[i] + 1, len(df_slice)):
                        k_low = low_series[k]
                        k_high = high_series[k]
                        k_close = close_series[k]
                        y_line = m * k + c
                        upper_bound = y_line + thickness
                        lower_bound = y_line - thickness
                        
                        if p_type == "support":
                            if k_low <= upper_bound:
                                in_zone = True
                            if in_zone and k_close > upper_bound:
                                touches += 1
                                in_zone = False
                        else: # resistance
                            if k_high >= lower_bound:
                                in_zone = True
                            if in_zone and k_close < lower_bound:
                                touches += 1
                                in_zone = False
                                
                    if touches >= self.min_touches:
                        valid_lines.append({
                            "m": float(m), "c": float(c), "rse": float(rse), 
                            "thickness": float(thickness), "touches": touches
                        })
            return valid_lines
            
        if self.signal_type in ("bullish", "any"):
            support_lines = find_valid_trendlines("support")
            for line in support_lines:
                y_line = line["m"] * current_idx + line["c"]
                upper_bound = y_line + line["thickness"]
                lower_bound = y_line - line["thickness"]
                
                # Check if current close is near the trendline support zone
                if lower_bound <= current_close <= upper_bound * 1.005:
                    passed = True
                    details["trendline_support"] = line
                    break

        if self.signal_type in ("bearish", "any"):
            resistance_lines = find_valid_trendlines("resistance")
            for line in resistance_lines:
                y_line = line["m"] * current_idx + line["c"]
                upper_bound = y_line + line["thickness"]
                lower_bound = y_line - line["thickness"]
                
                # Check if current close is near the trendline resistance zone
                if lower_bound * 0.995 <= current_close <= upper_bound:
                    passed = True
                    details["trendline_resistance"] = line
                    break

        return RuleResult(self.name, passed, current_close, None, details, self.weight)

    def to_dict(self) -> dict:
        return {
            "type": "TrendlineRule", 
            "lookback": self.lookback,
            "order": self.order, 
            "thickness_multiplier": self.thickness_multiplier,
            "min_touches": self.min_touches,
            "signal_type": self.signal_type
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(
            lookback=data.get("lookback", 200),
            order=data.get("order", 5),
            thickness_multiplier=data.get("thickness_multiplier", 1.0),
            min_touches=data.get("min_touches", 3),
            signal_type=data.get("signal_type", "any")
        )

```

---

## File: `./rules/serializer.py`

```python
"""
screener/rules/serializer.py
----------------------------
JSON rule tree serialization logic.
"""

import json
from typing import Any

from screener.rules.base import ScreenRule


class RuleSerializer:
    RULE_REGISTRY: dict[str, type[ScreenRule]] = {}

    @classmethod
    def register_rule(cls, rule_class: type[ScreenRule]):
        cls.RULE_REGISTRY[rule_class.__name__] = rule_class

    @classmethod
    def to_dict(cls, rule: ScreenRule) -> dict:
        """Recursive conversion of ScreenRule tree to dict."""
        return rule.to_dict()

    @classmethod
    def from_dict(cls, data: dict) -> ScreenRule:
        """Recursive reconstruction of ScreenRule tree from dict."""
        if "type" not in data:
            raise ValueError(f"Rule dict must contain 'type' key. Got: {data}")
        
        rule_type = data["type"]
        if rule_type not in cls.RULE_REGISTRY:
            raise ValueError(f"Unknown rule type: {rule_type}")
        
        rule_class = cls.RULE_REGISTRY[rule_type]
        
        # Handle composites specially because they require recursive parsing
        if rule_type in ("AndRule", "OrRule"):
            rules = [cls.from_dict(r) for r in data.get("rules", [])]
            return rule_class(rules)  # type: ignore
        elif rule_type == "NotRule":
            rule = cls.from_dict(data["rule"])
            return rule_class(rule)  # type: ignore
        elif rule_type == "AtLeastNRule":
            rules = [cls.from_dict(r) for r in data.get("rules", [])]
            n = data.get("n", 1)
            return rule_class(rules, n=n)  # type: ignore
        
        return rule_class.from_dict(data)

    @classmethod
    def to_json(cls, rule: ScreenRule) -> str:
        return json.dumps(cls.to_dict(rule), indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> ScreenRule:
        data = json.loads(json_str)
        return cls.from_dict(data)

def build_rule_from_dict(data: dict) -> ScreenRule:
    """Convenience function to build rule from dict."""
    return RuleSerializer.from_dict(data)

# Auto-register all known rules at import
from screener.rules.composite import AndRule, OrRule, NotRule, AtLeastNRule
from screener.rules.technical import RSIRule, MACDRule, EMARule, GoldenCrossRule, DeathCrossRule, SupertrendRule, BollingerSqueezeRule, RelativeStrengthRule, ADXRule, VWAPRule
from screener.rules.price import PriceBreakoutRule, NearSupportRule, NearResistanceRule, PriceRangeRule, HigherHighRule, LowerLowRule, HorizontalZoneRule, TrendlineRule
from screener.rules.volume import VolumeBreakoutRule, VolumeDeclineRule, VolumeRatioRule, AccumulationRule, DistributionRule
from screener.rules.pattern import CandlePatternRule, ChartPatternRule, TrendStructureRule
from screener.rules.strategy import StrategyRule, StrategyConfluenceRule

for rule_class in [
    AndRule, OrRule, NotRule, AtLeastNRule,
    RSIRule, MACDRule, EMARule, GoldenCrossRule, DeathCrossRule,
    SupertrendRule, BollingerSqueezeRule, RelativeStrengthRule, ADXRule, VWAPRule,
    PriceBreakoutRule, NearSupportRule, NearResistanceRule, PriceRangeRule, HigherHighRule, LowerLowRule, HorizontalZoneRule, TrendlineRule,
    VolumeBreakoutRule, VolumeDeclineRule, VolumeRatioRule, AccumulationRule, DistributionRule,
    CandlePatternRule, ChartPatternRule, TrendStructureRule,
    StrategyRule, StrategyConfluenceRule
    ]:
    RuleSerializer.register_rule(rule_class)

```

---

## File: `./rules/strategy.py`

```python
"""
screener/rules/strategy.py
--------------------------
Rules that integrate backtesting strategies into the screener.
"""

from typing import Any, Type, Union

import pandas as pd

from screener.base import RuleResult
from screener.rules.base import ScreenRule


class StrategyRule(ScreenRule):
    """
    Evaluates a specific trading strategy on the latest data.
    """
    def __init__(self, strategy_class: Union[str, Type[Any]], strategy_params: dict, signal_type: str = "long"):
        if isinstance(strategy_class, str):
            from strategies.registry import get_strategy_class
            self.strategy_class = get_strategy_class(strategy_class)
            self.strategy_name = strategy_class
        else:
            self.strategy_class = strategy_class
            self.strategy_name = strategy_class.__name__
            
        self.strategy_params = strategy_params
        self.signal_type = signal_type.lower()
        
        self.name = f"StrategyRule_{self.strategy_name}_{self.signal_type}"
        self.description = f"Checks if {self.strategy_name} generated a {self.signal_type} signal."
        self.min_bars_required = getattr(self.strategy_class, "MIN_WARMUP_BARS", 1)
        
    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        try:
            strategy = self.strategy_class(**self.strategy_params)
            signals_df = strategy.generate_signals(df)
        except Exception as e:
            return RuleResult(
                rule_name=self.name,
                passed=False,
                value=None,
                threshold=None,
                details={"error": f"Strategy validation/execution error: {e}"},
                weight=self.weight
            )
        
        if signals_df is None or signals_df.empty or "signal" not in signals_df.columns:
            return RuleResult(
                rule_name=self.name,
                passed=False,
                value=None,
                threshold=None,
                details={"error": "no signal column or empty dataframe"},
                weight=self.weight
            )
            
        last_row = signals_df.iloc[-1]
        latest_signal = last_row.get("signal", 0)
        
        if pd.isna(latest_signal):
            latest_signal = 0
        else:
            latest_signal = int(latest_signal)
        
        target_signal = 1 if self.signal_type == "long" else -1
        passed = (latest_signal == target_signal)
        
        details = {
            "signal_type": self.signal_type,
            "latest_signal": latest_signal,
        }
        
        if "signal_tag" in signals_df.columns:
            tag = last_row.get("signal_tag", "")
            if not pd.isna(tag):
                details["signal_tag"] = str(tag)
                
        if "confidence" in signals_df.columns:
            conf = last_row.get("confidence")
            if not pd.isna(conf):
                details["confidence"] = float(conf)
                
        if "reason" in signals_df.columns:
            reason = last_row.get("reason", "")
            if not pd.isna(reason):
                details["reason"] = str(reason)
                
        if "order_spec" in signals_df.columns:
            ospec = last_row.get("order_spec")
            if ospec is not None:
                details["order_type"] = ospec.order_type.name
                if getattr(ospec, "tag", None):
                    details["order_tag"] = ospec.tag
        
        # Use confidence as score/value if present
        value = float(details.get("confidence", latest_signal))
        
        return RuleResult(
            rule_name=self.name,
            passed=bool(passed),
            value=value,
            threshold=float(target_signal),
            details=details,
            weight=self.weight
        )


class StrategyConfluenceRule(ScreenRule):
    """
    Evaluates multiple StrategyRules and passes if at least n_required pass.
    """
    def __init__(self, strategies: list[StrategyRule], n_required: int):
        self.strategies = strategies
        self.n_required = n_required
        
        self.name = f"StrategyConfluence_{self.n_required}_{len(strategies)}"
        self.description = f"Requires {self.n_required} out of {len(strategies)} strategies to pass."
        self.min_bars_required = max((r.min_bars_required for r in strategies), default=1)

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        passed_count = 0
        details = {}
        
        for strat in self.strategies:
            res = strat.evaluate_safe(df)
            details[strat.name] = res.passed
            # Also propagate nested details if available
            if res.details:
                details[f"{strat.name}_details"] = res.details
                
            if res.passed:
                passed_count += 1
                
        passed = passed_count >= self.n_required
        score = passed_count / len(self.strategies) if self.strategies else 0.0
        
        details["passed_count"] = passed_count
        details["total_strategies"] = len(self.strategies)
        details["n_required"] = self.n_required
        
        return RuleResult(
            rule_name=self.name,
            passed=passed,
            value=score,
            threshold=float(self.n_required) / len(self.strategies) if self.strategies else 0.0,
            details=details,
            weight=self.weight
        )

```

---

## File: `./rules/technical.py`

```python
"""
screener/rules/technical.py
---------------------------
Technical indicator rules for the screener.
"""

import pandas as pd

from screener.base import RuleResult
from screener.rules.base import ScreenRule
from indicators.engine import IndicatorEngine

# Shared indicator engine for all rules
_engine = IndicatorEngine()


class RSIRule(ScreenRule):
    def __init__(self, period: int = 14, threshold: float = 30.0, direction: str = "below"):
        self.period = period
        self.threshold = threshold
        self.direction = direction
        self.name = f"RSI({period}) {direction} {threshold}"
        self.description = "Relative Strength Index filter"
        self.min_bars_required = period + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        rsi_series = _engine.compute("rsi", df["close"], period=self.period)
        val = rsi_series.iloc[-1]
        prev_val = rsi_series.iloc[-2] if len(rsi_series) > 1 else val

        if self.direction == "below":
            passed = val < self.threshold
        elif self.direction == "above":
            passed = val > self.threshold
        elif self.direction == "cross_above":
            passed = prev_val <= self.threshold and val > self.threshold
        elif self.direction == "cross_below":
            passed = prev_val >= self.threshold and val < self.threshold
        else:
            passed = False

        return RuleResult(self.name, bool(passed), float(val), self.threshold, {"prev_value": float(prev_val)}, self.weight)

    def to_dict(self) -> dict:
        return {
            "type": "RSIRule",
            "period": self.period,
            "threshold": self.threshold,
            "direction": self.direction
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(period=data.get("period", 14), threshold=data.get("threshold", 30.0), direction=data.get("direction", "below"))


class MACDRule(ScreenRule):
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9, condition: str = "cross_above"):
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self.condition = condition
        self.name = f"MACD({fast},{slow},{signal}) {condition}"
        self.description = "MACD filter"
        self.min_bars_required = slow + signal
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        # Assuming MACD returns DataFrame with 'macd', 'signal', 'histogram'
        macd_df = _engine.compute("macd", df["close"], fast_period=self.fast, slow_period=self.slow, signal_period=self.signal)
        macd = macd_df["macd"].iloc[-1]
        sig = macd_df["signal"].iloc[-1]
        hist = macd_df["histogram"].iloc[-1]

        prev_macd = macd_df["macd"].iloc[-2] if len(macd_df) > 1 else macd
        prev_sig = macd_df["signal"].iloc[-2] if len(macd_df) > 1 else sig

        if self.condition == "cross_above":
            passed = prev_macd <= prev_sig and macd > sig
        elif self.condition == "cross_below":
            passed = prev_macd >= prev_sig and macd < sig
        elif self.condition == "histogram_positive":
            passed = hist > 0
        elif self.condition == "histogram_negative":
            passed = hist < 0
        else:
            passed = False

        return RuleResult(self.name, bool(passed), float(macd), float(sig), {"histogram": float(hist)}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "MACDRule", "fast": self.fast, "slow": self.slow, "signal": self.signal, "condition": self.condition}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(fast=data.get("fast", 12), slow=data.get("slow", 26), signal=data.get("signal", 9), condition=data.get("condition", "cross_above"))


class EMARule(ScreenRule):
    def __init__(self, period: int = 20, condition: str = "price_above"):
        self.period = period
        self.condition = condition
        self.name = f"EMA({period}) {condition}"
        self.description = "Exponential Moving Average filter"
        self.min_bars_required = period
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        ema_series = _engine.compute("ema", df["close"], period=self.period)
        val = ema_series.iloc[-1]
        close = df["close"].iloc[-1]
        prev_close = df["close"].iloc[-2] if len(df) > 1 else close
        prev_val = ema_series.iloc[-2] if len(ema_series) > 1 else val

        if self.condition == "price_above":
            passed = close > val
        elif self.condition == "price_below":
            passed = close < val
        elif self.condition == "cross_above":
            passed = prev_close <= prev_val and close > val
        elif self.condition == "cross_below":
            passed = prev_close >= prev_val and close < val
        else:
            passed = False

        return RuleResult(self.name, bool(passed), float(val), float(close), {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "EMARule", "period": self.period, "condition": self.condition}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(period=data.get("period", 20), condition=data.get("condition", "price_above"))


class GoldenCrossRule(ScreenRule):
    def __init__(self, fast: int = 50, slow: int = 200):
        self.fast = fast
        self.slow = slow
        self.name = f"GoldenCross({fast},{slow})"
        self.description = "Golden Cross (Fast EMA crosses above Slow EMA)"
        self.min_bars_required = slow + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        fast_ema = _engine.compute("ema", df["close"], period=self.fast)
        slow_ema = _engine.compute("ema", df["close"], period=self.slow)
        
        val_f = fast_ema.iloc[-1]
        val_s = slow_ema.iloc[-1]
        prev_f = fast_ema.iloc[-2] if len(fast_ema) > 1 else val_f
        prev_s = slow_ema.iloc[-2] if len(slow_ema) > 1 else val_s

        passed = prev_f <= prev_s and val_f > val_s
        return RuleResult(self.name, bool(passed), float(val_f), float(val_s), {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "GoldenCrossRule", "fast": self.fast, "slow": self.slow}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(fast=data.get("fast", 50), slow=data.get("slow", 200))


class DeathCrossRule(ScreenRule):
    def __init__(self, fast: int = 50, slow: int = 200):
        self.fast = fast
        self.slow = slow
        self.name = f"DeathCross({fast},{slow})"
        self.description = "Death Cross (Fast EMA crosses below Slow EMA)"
        self.min_bars_required = slow + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        fast_ema = _engine.compute("ema", df["close"], period=self.fast)
        slow_ema = _engine.compute("ema", df["close"], period=self.slow)
        
        val_f = fast_ema.iloc[-1]
        val_s = slow_ema.iloc[-1]
        prev_f = fast_ema.iloc[-2] if len(fast_ema) > 1 else val_f
        prev_s = slow_ema.iloc[-2] if len(slow_ema) > 1 else val_s

        passed = prev_f >= prev_s and val_f < val_s
        return RuleResult(self.name, bool(passed), float(val_f), float(val_s), {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "DeathCrossRule", "fast": self.fast, "slow": self.slow}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(fast=data.get("fast", 50), slow=data.get("slow", 200))


class SupertrendRule(ScreenRule):
    def __init__(self, period: int = 10, multiplier: float = 3.0, direction: str = "bullish"):
        self.period = period
        self.multiplier = multiplier
        self.direction = direction
        self.name = f"Supertrend({period},{multiplier}) {direction}"
        self.description = "Supertrend filter"
        self.min_bars_required = period + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        st_df = _engine.compute("supertrend", df["high"], df["low"], df["close"], period=self.period, multiplier=self.multiplier)
        val = st_df["supertrend"].iloc[-1]
        dir_val = st_df["direction"].iloc[-1]
        prev_dir_val = st_df["direction"].iloc[-2] if len(st_df) > 1 else dir_val

        if self.direction == "bullish":
            passed = dir_val == 1
        elif self.direction == "bearish":
            passed = dir_val == -1
        elif self.direction == "flip_bullish":
            passed = prev_dir_val == -1 and dir_val == 1
        elif self.direction == "flip_bearish":
            passed = prev_dir_val == 1 and dir_val == -1
        else:
            passed = False

        return RuleResult(self.name, bool(passed), float(val), None, {"direction": int(dir_val)}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "SupertrendRule", "period": self.period, "multiplier": self.multiplier, "direction": self.direction}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(period=data.get("period", 10), multiplier=data.get("multiplier", 3.0), direction=data.get("direction", "bullish"))


class BollingerSqueezeRule(ScreenRule):
    def __init__(self, bb_period: int = 20, bb_std: float = 2.0, kc_period: int = 20, kc_mult: float = 1.5):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.kc_period = kc_period
        self.kc_mult = kc_mult
        self.name = f"BBSqueeze({bb_period},{bb_std},{kc_period},{kc_mult})"
        self.description = "Bollinger Bands inside Keltner Channels"
        self.min_bars_required = max(bb_period, kc_period) + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        bb = _engine.compute("bollinger_bands", df["close"], period=self.bb_period, std_dev=self.bb_std)
        # Assuming kc function exists. If not, we might need a fallback or calculate it here.
        try:
            kc = _engine.compute("keltner_channels", df, period=self.kc_period, multiplier=self.kc_mult)
            bb_up, bb_dn = bb["upper"].iloc[-1], bb["lower"].iloc[-1]
            kc_up, kc_dn = kc["upper"].iloc[-1], kc["lower"].iloc[-1]
            passed = (bb_up <= kc_up) and (bb_dn >= kc_dn)
        except Exception:
            # Fallback if keltner not available
            passed = False
            bb_up, kc_up = 0.0, 0.0

        return RuleResult(self.name, bool(passed), float(bb_up), float(kc_up), {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "BollingerSqueezeRule", "bb_period": self.bb_period, "bb_std": self.bb_std, "kc_period": self.kc_period, "kc_mult": self.kc_mult}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(bb_period=data.get("bb_period", 20), bb_std=data.get("bb_std", 2.0), kc_period=data.get("kc_period", 20), kc_mult=data.get("kc_mult", 1.5))


class RelativeStrengthRule(ScreenRule):
    def __init__(self, benchmark_df: pd.DataFrame, period: int = 55, threshold: float = 0.0, condition: str = "above"):
        self.benchmark_df = benchmark_df
        self.period = period
        self.threshold = threshold
        self.condition = condition
        self.name = f"RS({period}) {condition} {threshold}"
        self.description = "Mansfield Relative Strength"
        self.min_bars_required = period + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        if self.benchmark_df is None or self.benchmark_df.empty:
            return RuleResult(self.name, False, None, None, {"error": "Missing benchmark_df"}, self.weight)

        # align dates
        aligned = pd.merge(df["close"].rename("stock"), self.benchmark_df["close"].rename("bench"), left_index=True, right_index=True, how="inner")
        if len(aligned) < self.min_bars_required:
            return RuleResult(self.name, False, None, None, {"error": "Not enough aligned bars"}, self.weight)

        ratio = aligned["stock"] / aligned["bench"]
        ratio_sma = ratio.rolling(self.period).mean()
        rs = ((ratio / ratio_sma) - 1) * 100
        
        val = rs.iloc[-1]
        prev_val = rs.iloc[-2] if len(rs) > 1 else val

        if self.condition == "above":
            passed = val > self.threshold
        elif self.condition == "below":
            passed = val < self.threshold
        elif self.condition == "improving":
            passed = val > prev_val
        else:
            passed = False

        return RuleResult(self.name, passed, float(val), self.threshold, {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "RelativeStrengthRule", "period": self.period, "threshold": self.threshold, "condition": self.condition}
        # benchmark_df cannot be serialized directly easily, must be re-injected on load

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        # Note: benchmark_df must be set post-deserialization by the caller
        return cls(benchmark_df=pd.DataFrame(), period=data.get("period", 55), threshold=data.get("threshold", 0.0), condition=data.get("condition", "above"))


class ADXRule(ScreenRule):
    def __init__(self, period: int = 14, threshold: float = 25.0, condition: str = "above"):
        self.period = period
        self.threshold = threshold
        self.condition = condition
        self.name = f"ADX({period}) {condition} {threshold}"
        self.description = "Average Directional Index"
        self.min_bars_required = period * 2
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        adx_df = _engine.compute("adx", df["high"], df["low"], df["close"], period=self.period)
        val = adx_df["adx"].iloc[-1]
        plus_di = adx_df["plus_di"].iloc[-1]
        minus_di = adx_df["minus_di"].iloc[-1]

        if self.condition == "above":
            passed = val > self.threshold
        elif self.condition == "di_plus_above":
            passed = val > self.threshold and plus_di > minus_di
        elif self.condition == "di_minus_above":
            passed = val > self.threshold and minus_di > plus_di
        else:
            passed = False

        return RuleResult(self.name, passed, float(val), self.threshold, {"plus_di": float(plus_di), "minus_di": float(minus_di)}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "ADXRule", "period": self.period, "threshold": self.threshold, "condition": self.condition}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(period=data.get("period", 14), threshold=data.get("threshold", 25.0), condition=data.get("condition", "above"))


class VWAPRule(ScreenRule):
    def __init__(self, condition: str = "price_above"):
        self.condition = condition
        self.name = f"VWAP {condition}"
        self.description = "Volume Weighted Average Price"
        self.min_bars_required = 2
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        # Check if intraday by index
        if not pd.api.types.is_datetime64_any_dtype(df.index):
            return RuleResult(self.name, False, None, None, {"error": "VWAP requires datetime index"}, self.weight)
        
        if len(df) > 1:
            time_diff = df.index.to_series().diff().median()
            if time_diff >= pd.Timedelta(days=1):
                import logging
                logging.getLogger(__name__).warning(
                    "VWAPRule used on daily data: VWAP will reset each day, treating each day as a separate session."
                )
        
        # Calculate session VWAP
        v = df['volume']
        tp = (df['high'] + df['low'] + df['close']) / 3
        vwap = (tp * v).groupby(df.index.date).cumsum() / v.groupby(df.index.date).cumsum()
        
        val = vwap.iloc[-1]
        close = df["close"].iloc[-1]
        prev_val = vwap.iloc[-2] if len(vwap) > 1 else val
        prev_close = df["close"].iloc[-2] if len(df) > 1 else close

        if self.condition == "price_above":
            passed = close > val
        elif self.condition == "price_below":
            passed = close < val
        elif self.condition == "cross_above":
            passed = prev_close <= prev_val and close > val
        elif self.condition == "cross_below":
            passed = prev_close >= prev_val and close < val
        else:
            passed = False

        return RuleResult(self.name, passed, float(val), float(close), {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "VWAPRule", "condition": self.condition}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(condition=data.get("condition", "price_above"))

```

---

## File: `./rules/vcp.py`

```python
import numpy as np
import pandas as pd
from scipy.signal import find_peaks
import logging

from screener.base import RuleResult
from screener.rules.base import ScreenRule

logger = logging.getLogger(__name__)

class VCPRule(ScreenRule):
    """
    Volatility Contraction Pattern (VCP) rule based on Mark Minervini's methodology.
    Identifies a series of tightening price contractions accompanied by volume dry-ups.
    """
    name = "VolatilityContractionPattern"
    description = "Mark Minervini's Volatility Contraction Pattern (VCP)"
    
    def __init__(self, pivot_mode="setup", min_contractions=2, max_contractions=6, lookback_days=252, weight=1.0):
        self.pivot_mode = pivot_mode.lower() # "setup" or "breakout"
        self.min_contractions = min_contractions
        self.max_contractions = max_contractions
        self.lookback_days = lookback_days
        self.weight = weight
        self.min_bars_required = lookback_days
        
    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        if len(df) < self.lookback_days:
            return RuleResult(self.name, False, float(df["close"].iloc[-1]) if not df.empty else 0.0, None, {"error": "not enough data"}, self.weight)
            
        # 1. Base Identification
        # Slice to the lookback period
        df_lookback = df.tail(self.lookback_days).copy()
        
        # Find absolute max in this period
        abs_high_idx = df_lookback["high"].idxmax()
        
        # We only evaluate from the absolute high onwards
        df_base = df_lookback.loc[abs_high_idx:].copy()
        
        # We need enough days to form a meaningful base (e.g. at least 3 weeks = 15 days)
        if len(df_base) < 15:
            return RuleResult(self.name, False, float(df["close"].iloc[-1]), None, {"reason": "base too short"}, self.weight)
            
        # 2. Noise Filtering
        # Apply a 3-day EMA to smooth high and low prices
        df_base["smoothed_high"] = df_base["high"].ewm(span=3, adjust=False).mean()
        df_base["smoothed_low"] = df_base["low"].ewm(span=3, adjust=False).mean()
        
        # 3. Peak and Trough Detection using scipy
        # distance=4 means peaks must be at least 4 days apart
        highs = df_base["smoothed_high"].values
        lows = df_base["smoothed_low"].values
        
        # Find Peaks
        peaks, _ = find_peaks(highs, distance=4)
        
        # Find Troughs (invert lows)
        troughs, _ = find_peaks(-lows, distance=4)
        
        if len(peaks) == 0 or len(troughs) == 0:
            return RuleResult(self.name, False, float(df["close"].iloc[-1]), None, {"reason": "no peaks/troughs"}, self.weight)
            
        # Calculate sequential drops
        # We match each peak with the lowest trough that follows it before the next peak
        drops = []
        valid_peaks = []
        for i in range(len(peaks)):
            peak_idx = peaks[i]
            # Next peak idx or end of array
            next_peak_idx = peaks[i+1] if i + 1 < len(peaks) else len(df_base)
            
            # Find all troughs between this peak and the next
            troughs_in_range = [t for t in troughs if peak_idx < t < next_peak_idx]
            
            if troughs_in_range:
                # Find the lowest trough in this range
                lowest_trough_idx = min(troughs_in_range, key=lambda t: lows[t])
                peak_val = highs[peak_idx]
                trough_val = lows[lowest_trough_idx]
                
                drop_pct = (peak_val - trough_val) / peak_val
                drops.append(drop_pct)
                valid_peaks.append(peak_idx)
                
        # We need at least min_contractions drops
        if len(drops) < self.min_contractions or len(drops) > self.max_contractions:
            return RuleResult(self.name, False, float(df["close"].iloc[-1]), None, {"reason": f"{len(drops)} contractions"}, self.weight)
            
        # Check if drops are contracting: Drop 1 > Drop 2 > Drop 3...
        # Real-world data is messy, so we check if the overall trend of drops is decreasing
        is_contracting = True
        for i in range(len(drops) - 1):
            # Allow a small tolerance (e.g. 2%) for noise
            if drops[i+1] > drops[i] + 0.02: 
                is_contracting = False
                break
                
        if not is_contracting:
            return RuleResult(self.name, False, float(df["close"].iloc[-1]), None, {"reason": "drops not contracting"}, self.weight)
            
        # Check if final drop is tight enough (e.g. < 12%)
        final_drop = drops[-1]
        if final_drop > 0.12:
            return RuleResult(self.name, False, float(df["close"].iloc[-1]), None, {"reason": f"final drop too wide: {final_drop:.1%}"}, self.weight)
            
        # 4. Volume Drying Up
        # Average volume over the last 50 days
        vol_50ma = df["volume"].tail(50).mean()
        
        # Volume during the final contraction (from last valid peak to today)
        last_peak_idx = valid_peaks[-1]
        final_contraction_vol = df_base["volume"].iloc[last_peak_idx:].mean()
        
        if final_contraction_vol >= vol_50ma:
             # Look for at least one extreme dry up day instead
             min_vol_in_contraction = df_base["volume"].iloc[last_peak_idx:].min()
             if min_vol_in_contraction >= vol_50ma * 0.6:
                 return RuleResult(self.name, False, float(df["close"].iloc[-1]), None, {"reason": "volume not drying up"}, self.weight)
                 
        # 5. Pivot Point Logic
        # The exact high price of the final peak in our sequence
        pivot_point = df_base["high"].iloc[last_peak_idx]
        current_close = df["close"].iloc[-1]
        current_vol = df["volume"].iloc[-1]
        
        passed = False
        if self.pivot_mode == "setup":
            # Current price within -5% to +1% of pivot
            if (pivot_point * 0.95) <= current_close <= (pivot_point * 1.01):
                # Volume today should be relatively low (e.g. < 1.2 * 50 MA)
                if current_vol <= vol_50ma * 1.2: 
                    passed = True
        elif self.pivot_mode == "breakout":
            # Current price > pivot and volume > 1.5 * 50MA
            if current_close > pivot_point and current_vol > (vol_50ma * 1.5):
                passed = True
                
        # 6. Technical Footprint
        base_weeks = len(df_base) // 5
        max_drop = max(drops)
        footprint = f"{base_weeks}W {max_drop*100:.0f}/{final_drop*100:.0f} {len(drops)}T"
        
        details = {
            "pivot": float(pivot_point),
            "footprint": footprint,
            "drops": [float(d) for d in drops],
            "mode": self.pivot_mode
        }
        
        return RuleResult(self.name, passed, float(current_close), None, details, self.weight)

```

---

## File: `./rules/volume.py`

```python
"""
screener/rules/volume.py
------------------------
Volume Rules for the screener.
"""

import pandas as pd

from screener.base import RuleResult
from screener.rules.base import ScreenRule
from indicators.engine import IndicatorEngine

_engine = IndicatorEngine()


class VolumeBreakoutRule(ScreenRule):
    def __init__(self, period: int = 20, multiplier: float = 2.0):
        self.period = period
        self.multiplier = multiplier
        self.name = f"VolBreakout({period},{multiplier})"
        self.description = "Volume > multiplier * average volume"
        self.min_bars_required = period + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        vol = df["volume"].iloc[-1]
        avg_vol = df["volume"].iloc[-self.period-1:-1].mean()
        
        passed = vol > avg_vol * self.multiplier
        return RuleResult(self.name, bool(passed), float(vol), float(avg_vol * self.multiplier), {"avg_vol": float(avg_vol)}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "VolumeBreakoutRule", "period": self.period, "multiplier": self.multiplier}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(period=data.get("period", 20), multiplier=data.get("multiplier", 2.0))


class VolumeDeclineRule(ScreenRule):
    def __init__(self, period: int = 20, multiplier: float = 0.5):
        self.period = period
        self.multiplier = multiplier
        self.name = f"VolDecline({period},{multiplier})"
        self.description = "Volume < multiplier * average volume (drying up)"
        self.min_bars_required = period + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        vol = df["volume"].iloc[-1]
        avg_vol = df["volume"].iloc[-self.period-1:-1].mean()
        
        passed = vol < avg_vol * self.multiplier
        return RuleResult(self.name, bool(passed), float(vol), float(avg_vol * self.multiplier), {"avg_vol": float(avg_vol)}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "VolumeDeclineRule", "period": self.period, "multiplier": self.multiplier}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(period=data.get("period", 20), multiplier=data.get("multiplier", 0.5))


class VolumeRatioRule(ScreenRule):
    def __init__(self, period: int = 20, threshold: float = 1.5):
        self.period = period
        self.threshold = threshold
        self.name = f"VolRatio({period})>{threshold}"
        self.description = "Ratio of today's volume to average volume"
        self.min_bars_required = period + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        vol = df["volume"].iloc[-1]
        avg_vol = df["volume"].iloc[-self.period-1:-1].mean()
        ratio = vol / avg_vol if avg_vol > 0 else 0.0
        
        passed = ratio > self.threshold
        return RuleResult(self.name, bool(passed), float(ratio), self.threshold, {"avg_vol": float(avg_vol)}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "VolumeRatioRule", "period": self.period, "threshold": self.threshold}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(period=data.get("period", 20), threshold=data.get("threshold", 1.5))


class AccumulationRule(ScreenRule):
    def __init__(self, period: int = 10):
        self.period = period
        self.name = f"Accumulation({period})"
        self.description = "Price up + volume up = accumulation signal"
        self.min_bars_required = period + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        obv = _engine.compute("obv", df["close"], df["volume"])
        obv_val = obv.iloc[-1]
        obv_ma = obv.rolling(self.period).mean().iloc[-1]
        
        passed = obv_val > obv_ma
        return RuleResult(self.name, bool(passed), float(obv_val), float(obv_ma), {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "AccumulationRule", "period": self.period}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(period=data.get("period", 10))


class DistributionRule(ScreenRule):
    def __init__(self, period: int = 10):
        self.period = period
        self.name = f"Distribution({period})"
        self.description = "Price down + volume up = distribution signal"
        self.min_bars_required = period + 1
        self.weight = 1.0

    def evaluate(self, df: pd.DataFrame) -> RuleResult:
        obv = _engine.compute("obv", df["close"], df["volume"])
        obv_val = obv.iloc[-1]
        obv_ma = obv.rolling(self.period).mean().iloc[-1]
        
        passed = obv_val < obv_ma
        return RuleResult(self.name, passed, float(obv_val), float(obv_ma), {}, self.weight)

    def to_dict(self) -> dict:
        return {"type": "DistributionRule", "period": self.period}

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRule":
        return cls(period=data.get("period", 10))

```

---

