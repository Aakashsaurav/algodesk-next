## File: `./__init__.py`

```python
"""
indicators package
==================

Canonical modular indicator layer for AlgoDesk.

The package is intentionally lazy so ``import indicators`` does not eagerly
import the heavier modules until one of their attributes is requested.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    # Engine
    "IndicatorEngine": ("indicators.engine", "IndicatorEngine"),
    "IndicatorSpec": ("indicators.engine", "IndicatorSpec"),
    # Bridge
    "IndicatorBridge": ("indicators.bridge", "IndicatorBridge"),
    "LibraryStatus": ("indicators.bridge", "LibraryStatus"),
    # Canonical modular indicators
    "sma": ("indicators.moving_averages", "sma"),
    "ema": ("indicators.moving_averages", "ema"),
    "dema": ("indicators.moving_averages", "dema"),
    "wma": ("indicators.moving_averages", "wma"),
    "vwap": ("indicators.volume", "vwap"),
    "rsi": ("indicators.oscillators", "rsi"),
    "stochastic": ("indicators.oscillators", "stochastic"),
    "macd": ("indicators.oscillators", "macd"),
    "roc": ("indicators.oscillators", "roc"),
    "cci": ("indicators.oscillators", "cci"),
    "atr": ("indicators.volatility", "atr"),
    "bollinger_bands": ("indicators.volatility", "bollinger_bands"),
    "keltner_channels": ("indicators.volatility", "keltner_channels"),
    "bb_squeeze": ("indicators.volatility", "bb_squeeze"),
    "supertrend": ("indicators.trend", "supertrend"),
    "adx": ("indicators.trend", "adx"),
    "obv": ("indicators.volume", "obv"),
    "zscore": ("indicators.statistics", "zscore"),
    "rolling_correlation": ("indicators.statistics", "rolling_correlation"),
    "rolling_beta": ("indicators.statistics", "rolling_beta"),
    "spread": ("indicators.statistics", "spread"),
    "half_life": ("indicators.statistics", "half_life"),
    "cointegration_test": ("indicators.statistics", "cointegration_test"),
    "crossover": ("indicators.helpers", "crossover"),
    "crossunder": ("indicators.helpers", "crossunder"),
    "above_threshold": ("indicators.helpers", "above_threshold"),
    "below_threshold": ("indicators.helpers", "below_threshold"),
    "candle_body": ("indicators.helpers", "candle_body"),
    "candle_range": ("indicators.helpers", "candle_range"),
    "is_green": ("indicators.helpers", "is_green"),
    "is_red": ("indicators.helpers", "is_red"),
    "upper_shadow": ("indicators.helpers", "upper_shadow"),
    "lower_shadow": ("indicators.helpers", "lower_shadow"),
    "relative_strength": ("indicators.relative_strength", "relative_strength"),
    "relative_strength_rank": ("indicators.relative_strength", "relative_strength_rank"),
    # Future namespaces
    "IndicatorRegistry": ("indicators.registry", "IndicatorRegistry"),
    "ExpressionParser": ("indicators.expression", "ExpressionParser"),
    "CustomIndicatorSpec": ("indicators.custom", "CustomIndicatorSpec"),
    "CustomIndicatorLoader": ("indicators.custom", "CustomIndicatorLoader"),
    "scan_all_candlestick": ("indicators.patterns", "scan_all_candlestick"),
    "scan_all_chart": ("indicators.patterns", "scan_all_chart"),
    "trend_structure": ("indicators.patterns", "trend_structure"),
}


def __getattr__(name: str) -> Any:
    if name not in _LAZY_EXPORTS:
        raise AttributeError(f"module 'indicators' has no attribute {name!r}")
    module_name, attr_name = _LAZY_EXPORTS[name]
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | set(_LAZY_EXPORTS.keys()))


__all__ = sorted(_LAZY_EXPORTS.keys())

```

---

## File: `./bridge.py`

```python
"""
indicators/bridge.py
---------------------
Universal indicator bridge — use ANY indicator library in your strategies.

THEORY — WHY A BRIDGE?
========================
Our system has its own modular pure-Python indicators in `indicators/`.
But the world has thousands of battle-tested indicators in libraries like:
  • pandas-ta  — 130+ indicators, pip install pandas-ta
  • TA-Lib     — C-based, blazing fast, pip install TA-Lib
  • ta          — lightweight, pip install ta

This bridge lets your strategy use indicators from ANY of these sources
transparently, with a uniform interface and graceful fallback:

  1. If the requested library is available → use it directly
  2. If not available → fall back to our built-in modular indicators
  3. If no fallback exists → raise a clear, actionable error

USAGE IN A STRATEGY:
    from indicators.bridge import IndicatorBridge
    bridge = IndicatorBridge()

    # Will use pandas-ta if installed, else our built-in:
    rsi = bridge.rsi(df['close'], 14)

    # Explicitly use pandas-ta:
    rsi = bridge.rsi(df['close'], 14, library='pandas_ta')

    # Explicitly use talib:
    atr = bridge.atr(df, 14, library='talib')

    # Use any pandas-ta indicator by name:
    result = bridge.pandas_ta_indicator(df, 'bbands', length=20, std=2)

    # Use any talib indicator by name:
    result = bridge.talib_indicator(df['close'], 'DEMA', timeperiod=20)

    # Register your own custom indicator:
    def my_custom_rsi(close, period):
        # your logic here
        return ...
    bridge.register('my_rsi', my_custom_rsi)
    rsi = bridge.call('my_rsi', df['close'], 14)
"""

import importlib
import logging
import threading
from typing import Any, Callable, Dict, Optional

import numpy as np
import pandas as pd
from indicators.registry import register_indicator

logger = logging.getLogger(__name__)


# =============================================================================
# Library Detection
# =============================================================================

def _is_available(library_name: str) -> bool:
    """Check if a library is installed without importing it (fast check)."""
    try:
        importlib.import_module(library_name)
        return True
    except ImportError:
        return False


class LibraryStatus:
    """
    Checks and caches the availability of external indicator libraries.
    This is evaluated once at startup — no repeated import overhead.
    """
    _cache: Dict[str, bool] = {}
    _lock = threading.RLock()

    @classmethod
    def check(cls, name: str) -> bool:
        with cls._lock:
            if name not in cls._cache:
                cls._cache[name] = _is_available(name)
                if cls._cache[name]:
                    logger.debug("Library available: %s", name)
                else:
                    logger.debug("Library NOT available: %s (will use fallback)", name)
            return cls._cache[name]

    @classmethod
    def summary(cls) -> str:
        """Print a summary of which indicator libraries are available."""
        lines = ["", "=" * 50, "  INDICATOR LIBRARY STATUS", "=" * 50]
        for lib in ["pandas_ta", "talib", "ta"]:
            status = "✅ Available" if cls.check(lib) else "❌ Not installed"
            lines.append(f"  {lib:<15}: {status}")
        lines.append("  (built-in)     : ✅ Always available")
        lines.append("=" * 50)
        return "\n".join(lines)


# =============================================================================
# IndicatorBridge — main class
# =============================================================================

class IndicatorBridge:
    """
    Universal indicator bridge.

    Provides:
      - Built-in wrappers for common indicators (rsi, ema, atr, etc.)
        with library fallback chain: preferred → pandas_ta → talib → built-in
      - Direct access to pandas_ta / talib by indicator name
      - Custom indicator registration
    """
    __slots__ = ["preferred", "_custom"]

    def __init__(self, preferred_library: str = "auto"):
        """
        Args:
            preferred_library: 'auto' (detect best available), 'pandas_ta',
                               'talib', 'built_in', or 'ta'
        """
        self.preferred = preferred_library
        self._custom: Dict[str, Callable] = {}   # User-registered indicators
        self._log_library_status()

    def _log_library_status(self):
        """Log which libraries are available at construction time."""
        available = []
        for lib in ["pandas_ta", "talib", "ta"]:
            if LibraryStatus.check(lib):
                available.append(lib)
        available.append("built_in")
        logger.info("IndicatorBridge: available libraries = %s", available)

    def _best_library(self) -> str:
        """Return the best available library based on preference."""
        if self.preferred != "auto":
            return self.preferred
        for lib in ["pandas_ta", "talib", "built_in"]:
            if lib == "built_in" or LibraryStatus.check(lib):
                return lib
        return "built_in"

    # -------------------------------------------------------------------------
    # Custom indicator registration
    # -------------------------------------------------------------------------

    def register(self, name: str, func: Callable) -> None:
        """
        Register a custom indicator function.

        Args:
            name: Name to call it by (e.g. 'my_rsi')
            func: Callable that accepts (data, *args, **kwargs) and returns Series

        Example:
            bridge.register('zscore_rsi', lambda c, p: zscore(rsi(c, p), 20))
            values = bridge.call('zscore_rsi', df['close'], 14)
        """
        self._custom[name] = func
        logger.info("Registered custom indicator: '%s'", name)

    def call(self, name: str, *args, **kwargs) -> Any:
        """
        Call a registered custom indicator by name.

        Raises:
            KeyError: if the indicator name is not registered
        """
        if name not in self._custom:
            raise KeyError(
                f"Custom indicator '{name}' not registered. "
                f"Available: {list(self._custom.keys())}"
            )
        return self._custom[name](*args, **kwargs)

    # -------------------------------------------------------------------------
    # Direct library access
    # -------------------------------------------------------------------------

    def pandas_ta_indicator(
        self,
        df:        pd.DataFrame,
        indicator: str,
        **kwargs
    ) -> Any:
        """
        Call any pandas-ta indicator directly by name.

        Args:
            df:        OHLCV DataFrame (pandas-ta uses df.ta accessor)
            indicator: Indicator name string (e.g. 'bbands', 'ema', 'rsi')
            **kwargs:  Parameters passed to the indicator

        Returns:
            DataFrame or Series with indicator values

        Raises:
            ImportError: if pandas-ta is not installed

        Example:
            bb = bridge.pandas_ta_indicator(df, 'bbands', length=20, std=2)
            df['bb_upper'] = bb['BBU_20_2.0']
        """
        if not LibraryStatus.check("pandas_ta"):
            raise ImportError(
                "pandas-ta is not installed. Run: pip install pandas-ta\n"
                "Or use bridge.rsi() which falls back to built-in automatically."
            )
        import pandas_ta as pta
        func = getattr(pta, indicator, None)
        if func is None:
            raise ValueError(
                f"pandas-ta does not have indicator: '{indicator}'. "
                f"See: https://github.com/twopirllc/pandas-ta#indicators"
            )
        return func(
            close=df.get("close"),
            high=df.get("high"),
            low=df.get("low"),
            open_=df.get("open"),
            volume=df.get("volume"),
            **kwargs
        )

    def talib_indicator(
        self,
        data,
        indicator: str,
        **kwargs
    ) -> Any:
        """
        Call any TA-Lib indicator directly by name.

        Args:
            data:      Series (for single-input) or DataFrame (for OHLCV)
            indicator: TA-Lib function name (e.g. 'RSI', 'BBANDS', 'ATR')
            **kwargs:  Parameters passed to the function

        Returns:
            ndarray or tuple of ndarrays

        Raises:
            ImportError: if TA-Lib is not installed

        Example:
            upper, mid, lower = bridge.talib_indicator(df, 'BBANDS', timeperiod=20)
        """
        if not LibraryStatus.check("talib"):
            raise ImportError(
                "TA-Lib is not installed. Run: pip install TA-Lib\n"
                "(Note: TA-Lib requires C library — see talib.github.io for setup)"
            )
        import talib
        func = getattr(talib, indicator.upper(), None)
        if func is None:
            raise ValueError(
                f"TA-Lib does not have function: '{indicator}'. "
                f"See: https://ta-lib.github.io/ta-lib-python/doc_index.html"
            )
        if isinstance(data, pd.DataFrame):
            return func(
                data.get("close", data.iloc[:, 0]).values.astype(float),
                **kwargs
            )
        return func(data.values.astype(float) if hasattr(data, "values") else data,
                    **kwargs)

    # -------------------------------------------------------------------------
    # Common indicators with library fallback chain
    # -------------------------------------------------------------------------
    @register_indicator(
        name="rsi", category="momentum", inputs=["close"], outputs=["rsi"],
        parameters={"period": 14}
    )
    def rsi(self, close: pd.Series, period: int = 14,
            library: str = "auto") -> pd.Series:
        """
        RSI — Relative Strength Index.

        Fallback chain: pandas_ta → talib → built_in
        """
        lib = library if library != "auto" else self._best_library()

        if lib == "pandas_ta" and LibraryStatus.check("pandas_ta"):
            import pandas_ta as pta
            result = pta.rsi(close, length=period)
            return result.rename(f"RSI_{period}")

        if lib == "talib" and LibraryStatus.check("talib"):
            import talib
            arr = talib.RSI(close.values.astype(float), timeperiod=period)
            return pd.Series(arr, index=close.index, name=f"RSI_{period}")

        # Built-in fallback
        from indicators.oscillators import rsi as _rsi
        return _rsi(close, period)

    @register_indicator(
        name="ema", category="trend", inputs=["close"], outputs=["ema"],
        parameters={"period": 20}
    )
    def ema(self, close: pd.Series, period: int,
            library: str = "auto") -> pd.Series:
        """EMA — Exponential Moving Average."""
        lib = library if library != "auto" else self._best_library()

        if lib == "pandas_ta" and LibraryStatus.check("pandas_ta"):
            import pandas_ta as pta
            return pta.ema(close, length=period).rename(f"EMA_{period}")

        if lib == "talib" and LibraryStatus.check("talib"):
            import talib
            arr = talib.EMA(close.values.astype(float), timeperiod=period)
            return pd.Series(arr, index=close.index, name=f"EMA_{period}")

        from indicators.moving_averages import ema as _ema
        return _ema(close, period)

    @register_indicator(
        name="sma", category="trend", inputs=["close"], outputs=["sma"],
        parameters={"period": 20}
    )
    def sma(self, close: pd.Series, period: int,
            library: str = "auto") -> pd.Series:
        """SMA — Simple Moving Average."""
        lib = library if library != "auto" else self._best_library()

        if lib == "pandas_ta" and LibraryStatus.check("pandas_ta"):
            import pandas_ta as pta
            return pta.sma(close, length=period).rename(f"SMA_{period}")

        if lib == "talib" and LibraryStatus.check("talib"):
            import talib
            arr = talib.SMA(close.values.astype(float), timeperiod=period)
            return pd.Series(arr, index=close.index, name=f"SMA_{period}")

        from indicators.moving_averages import sma as _sma
        return _sma(close, period)

    @register_indicator(
        name="atr", category="volatility", inputs=["high", "low", "close"], outputs=["atr"],
        parameters={"period": 14}
    )
    def atr(self, df: pd.DataFrame, period: int = 14,
            library: str = "auto") -> pd.Series:
        """ATR — Average True Range."""
        lib = library if library != "auto" else self._best_library()

        if lib == "pandas_ta" and LibraryStatus.check("pandas_ta"):
            import pandas_ta as pta
            return pta.atr(df["high"], df["low"], df["close"], length=period).rename("atr")

        if lib == "talib" and LibraryStatus.check("talib"):
            import talib
            arr = talib.ATR(
                df["high"].values.astype(float),
                df["low"].values.astype(float),
                df["close"].values.astype(float),
                timeperiod=period,
            )
            return pd.Series(arr, index=df.index, name=f"ATR_{period}")

        from indicators.volatility import atr as _atr
        return _atr(df["high"], df["low"], df["close"], period)

    @register_indicator(
        name="macd", category="momentum", inputs=["close"], outputs=["macd", "histogram", "signal"],
        parameters={"fast": 12, "slow": 26, "signal": 9}
    )
    def macd(self, close: pd.Series, fast: int = 12, slow: int = 26,
             signal: int = 9, library: str = "auto") -> pd.DataFrame:
        """MACD — Returns DataFrame with macd, signal, histogram columns."""
        lib = library if library != "auto" else self._best_library()

        if lib == "pandas_ta" and LibraryStatus.check("pandas_ta"):
            import pandas_ta as pta
            result = pta.macd(close, fast=fast, slow=slow, signal=signal)
            # pandas-ta returns columns like MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
            # P3 FIX: removed dead `cols` variable (was assigned but never read)
            result.columns = ["macd", "histogram", "signal"]
            return result

        if lib == "talib" and LibraryStatus.check("talib"):
            import talib
            m, s, h = talib.MACD(
                close.values.astype(float),
                fastperiod=fast, slowperiod=slow, signalperiod=signal
            )
            return pd.DataFrame({
                "macd": m, "histogram": h, "signal": s
            }, index=close.index)

        from indicators.oscillators import macd as _macd
        return _macd(close, fast, slow, signal)

    @register_indicator(
        name="bollinger_bands", category="volatility", inputs=["close"], outputs=["bb_lower", "bb_middle", "bb_upper", "bb_bandwidth", "bb_pct_b"],
        parameters={"period": 20, "std_dev": 2.0}
    )
    def bollinger_bands(self, close: pd.Series, period: int = 20,
                        std_dev: float = 2.0,
                        library: str = "auto") -> pd.DataFrame:
        """Bollinger Bands — Returns DataFrame with bb_upper, bb_middle, bb_lower."""

        lib = library if library != "auto" else self._best_library()

        if lib == "pandas_ta" and LibraryStatus.check("pandas_ta"):
            import pandas_ta as pta
            result = pta.bbands(close, length=period, std=std_dev)
            result.columns = ["bb_lower", "bb_middle", "bb_upper",
                               "bb_bandwidth", "bb_pct_b"]
            return result

        if lib == "talib" and LibraryStatus.check("talib"):
            import talib
            upper, mid, lower = talib.BBANDS(
                close.values.astype(float),
                timeperiod=period,
                nbdevup=std_dev, nbdevdn=std_dev,
            )
            df = pd.DataFrame({
                "bb_upper": upper, "bb_middle": mid, "bb_lower": lower
            }, index=close.index)
            df["bb_bandwidth"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_middle"]
            df["bb_pct_b"]     = ((close - df["bb_lower"]) /
                                   (df["bb_upper"] - df["bb_lower"]))
            return df

        from indicators.volatility import bollinger_bands as _bb
        return _bb(close, period, std_dev)

    @register_indicator(
        name="supertrend", category="trend", inputs=["high", "low", "close"], outputs=["supertrend", "direction", "buy_signal", "sell_signal"],
        parameters={"period": 10, "multiplier": 3.0}
    )
    def supertrend(self, df: pd.DataFrame, period: int = 10,
                   multiplier: float = 3.0,
                   library: str = "auto") -> pd.DataFrame:
        """Supertrend — always uses built-in (pandas-ta version differs)."""
        from indicators.trend import supertrend as _st
        return _st(df["high"], df["low"], df["close"], period, multiplier)

    @register_indicator(
        name="vwap", category="volume", inputs=["high", "low", "close", "volume"], outputs=["vwap"],
        parameters={}
    )
    def vwap(self, df: pd.DataFrame, library: str = "auto") -> pd.Series:
        """VWAP — Volume Weighted Average Price (daily reset)."""
        if library == "pandas_ta" and LibraryStatus.check("pandas_ta"):
            import pandas_ta as pta
            return pta.vwap(df["high"], df["low"], df["close"], df["volume"])

        from indicators.volume import vwap as _vwap
        return _vwap(df["high"], df["low"], df["close"], df["volume"])

    @register_indicator(
        name="stochastic", category="momentum", inputs=["high", "low", "close"], outputs=["stoch_k", "stoch_d"],
        parameters={"k_period": 14, "d_period": 3}
    )
    def stochastic(self, df: pd.DataFrame, k_period: int = 14,
                   d_period: int = 3, library: str = "auto") -> pd.DataFrame:
        """Stochastic Oscillator — %K and %D."""
        if library == "talib" and LibraryStatus.check("talib"):
            import talib
            k, d = talib.STOCH(
                df["high"].values.astype(float),
                df["low"].values.astype(float),
                df["close"].values.astype(float),
                fastk_period=k_period, slowd_period=d_period,
            )
            return pd.DataFrame({
                "stoch_k": k, "stoch_d": d
            }, index=df.index)

        from indicators.oscillators import stochastic as _stoch
        return _stoch(df["high"], df["low"], df["close"], k_period, d_period)

    @register_indicator(
        name="zscore", category="statistics", inputs=["close"], outputs=["zscore"],
        parameters={"period": 20}
    )
    def zscore(self, series: pd.Series, period: int = 20) -> pd.Series:
        """Rolling Z-Score — always uses built-in."""
        from indicators.statistics import zscore as _z
        return _z(series, period)

    @register_indicator(
        name="dema", category="trend", inputs=["close"], outputs=["dema"],
        parameters={"period": 14}
    )
    def dema(self, close: pd.Series, period: int) -> pd.Series:
        """DEMA — Double Exponential Moving Average."""
        from indicators.moving_averages import dema as _dema
        return _dema(close, period)

    @register_indicator(
        name="wma", category="trend", inputs=["close"], outputs=["wma"],
        parameters={"period": 14}
    )
    def wma(self, close: pd.Series, period: int) -> pd.Series:
        """WMA — Weighted Moving Average."""
        from indicators.moving_averages import wma as _wma
        return _wma(close, period)

    @register_indicator(
        name="cci", category="momentum", inputs=["high", "low", "close"], outputs=["cci"],
        parameters={"period": 20}
    )
    def cci(self, high: pd.Series, low: pd.Series, close: pd.Series, period: int = 20) -> pd.Series:
        """CCI — Commodity Channel Index."""
        from indicators.oscillators import cci as _cci
        return _cci(high, low, close, period)

    @register_indicator(
        name="roc", category="momentum", inputs=["close"], outputs=["roc"],
        parameters={"period": 12}
    )
    def roc(self, close: pd.Series, period: int = 12) -> pd.Series:
        """ROC — Rate of Change."""
        from indicators.oscillators import roc as _roc
        return _roc(close, period)

    # -------------------------------------------------------------------------
    # Status helpers
    # -------------------------------------------------------------------------

    def library_status(self) -> str:
        """Print a formatted summary of which indicator libraries are available."""
        return LibraryStatus.summary()

    def list_custom(self) -> list:
        """List names of all registered custom indicators."""
        return list(self._custom.keys())
```

---

## File: `./custom.py`

```python
"""
indicators/custom.py
--------------------
Loader and manager for user-defined custom indicators.
Supports storing custom indicators in an SQLite database and loading them into the engine.
"""

import sqlite3
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List, Optional

import pandas as pd
from indicators.engine import IndicatorEngine
from indicators.expression import ExpressionParser
from config import config

logger = logging.getLogger(__name__)


@dataclass
class CustomIndicatorSpec:
    """Specification for a user-defined custom indicator."""
    name: str
    category: str
    description: str
    type: str  # 'expression' or 'python'
    code: str
    inputs: List[str]
    outputs: List[str]
    parameters: Dict[str, Any]


class CustomIndicatorLoader:
    """
    Manages persistence of custom indicators to SQLite and loads them into
    the IndicatorEngine at runtime.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or config.CUSTOM_INDICATOR
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the SQLite database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS custom_indicators (
                    name TEXT PRIMARY KEY,
                    category TEXT,
                    description TEXT,
                    type TEXT,
                    code TEXT,
                    inputs TEXT,
                    outputs TEXT,
                    parameters TEXT
                )
            ''')
            conn.commit()

    def save(self, spec: CustomIndicatorSpec) -> None:
        """
        Save or update a custom indicator in the database.
        
        Raises:
            sqlite3.Error: If database operation fails.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO custom_indicators 
                (name, category, description, type, code, inputs, outputs, parameters)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                spec.name,
                spec.category,
                spec.description,
                spec.type,
                spec.code,
                json.dumps(spec.inputs),
                json.dumps(spec.outputs),
                json.dumps(spec.parameters)
            ))
            conn.commit()
        logger.info("Saved custom indicator '%s' to %s", spec.name, self.db_path)

    def delete(self, name: str) -> None:
        """
        Delete a custom indicator from the database.
        
        Raises:
            sqlite3.Error: If database operation fails.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('DELETE FROM custom_indicators WHERE name = ?', (name,))
            conn.commit()
        logger.info("Deleted custom indicator '%s'", name)

    def list_all(self) -> List[CustomIndicatorSpec]:
        """
        List all custom indicators stored in the database.
        
        Raises:
            sqlite3.Error: If database operation fails.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('SELECT * FROM custom_indicators')
            rows = cursor.fetchall()
        
        specs = []
        for row in rows:
            specs.append(CustomIndicatorSpec(
                name=row[0],
                category=row[1],
                description=row[2],
                type=row[3],
                code=row[4],
                inputs=json.loads(row[5]),
                outputs=json.loads(row[6]),
                parameters=json.loads(row[7])
            ))
        return specs

    def load_into_engine(self, engine: IndicatorEngine) -> None:
        """
        Load all custom indicators from the database into the given engine.
        
        Raises:
            sqlite3.Error: If database operation fails when loading indicators.
        """
        specs = self.list_all()
        for spec in specs:
            if spec.type == 'expression':
                self._register_expression(engine, spec)
            elif spec.type == 'python':
                self._register_python(engine, spec)
            else:
                logger.warning("Unknown custom indicator type '%s' for '%s'", spec.type, spec.name)
        logger.info("Loaded %d custom indicators into engine.", len(specs))

    def _register_expression(self, engine: IndicatorEngine, spec: CustomIndicatorSpec) -> None:
        """Wrap an AST expression as a callable indicator function."""
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Map positional args to inputs
            data = {}
            if len(args) == 1 and isinstance(args[0], dict):
                data.update(args[0])
            elif len(args) == 1 and isinstance(args[0], pd.DataFrame):
                data.update({col: args[0][col] for col in args[0].columns})
            else:
                for i, val in enumerate(args):
                    if i < len(spec.inputs):
                        data[spec.inputs[i]] = val
            
            # Use kwargs for any overrides (mostly data fields)
            data.update({k: v for k, v in kwargs.items() if k in spec.inputs})
            
            # Combine params from defaults + kwargs
            params = spec.parameters.copy()
            params.update({k: v for k, v in kwargs.items() if k not in spec.inputs})
            
            # Expressions might need params as 'data' variables if referenced
            data.update(params)

            parser = ExpressionParser(engine, data)
            result = parser.evaluate(spec.code)
            
            if not isinstance(result, pd.Series):
                raise TypeError(f"Custom expression indicator '{spec.name}' must return a pandas Series, got {type(result).__name__}.")
            
            for val in data.values():
                if isinstance(val, pd.Series):
                    if not result.index.equals(val.index):
                        raise ValueError(f"Custom expression indicator '{spec.name}' returned Series with mismatched index.")
                    break
                    
            return result
            
        wrapper.__name__ = spec.name
        wrapper.__doc__ = spec.description
        engine.register(spec.name, wrapper)

    def _register_python(self, engine: IndicatorEngine, spec: CustomIndicatorSpec) -> None:
        """
        Restricted Python execution environment for indicators.
        Only allows pandas, numpy, and basic math operations.
        """
        allowed_globals = {
            "pd": pd,
            "np": __import__("numpy"),
            "__builtins__": {
                "abs": abs, "min": min, "max": max, "sum": sum, 
                "round": round, "len": len, "range": range,
                "bool": bool, "int": int, "float": float, "str": str
            }
        }
        
        local_vars = {}
        try:
            # Code should define a function with the same name as spec.name
            exec(spec.code, allowed_globals, local_vars)
            func = local_vars.get(spec.name)
            
            if not callable(func):
                logger.error("Python indicator '%s' must define a function named '%s'", spec.name, spec.name)
                return
                
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                result = func(*args, **kwargs)
                
                if not isinstance(result, pd.Series):
                    raise TypeError(f"Custom python indicator '{spec.name}' must return a pandas Series, got {type(result).__name__}.")
                
                ref_index = None
                for arg in args:
                    if isinstance(arg, pd.Series) or isinstance(arg, pd.DataFrame):
                        ref_index = arg.index
                        break
                if ref_index is None:
                    for v in kwargs.values():
                        if isinstance(v, pd.Series) or isinstance(v, pd.DataFrame):
                            ref_index = v.index
                            break
                            
                if ref_index is not None and not result.index.equals(ref_index):
                    raise ValueError(f"Custom python indicator '{spec.name}' returned Series with mismatched index.")
                    
                return result
                
            engine.register(spec.name, wrapper)
        except Exception as e:
            logger.error("Failed to load python indicator '%s': %s", spec.name, e)

__all__ = ["CustomIndicatorSpec", "CustomIndicatorLoader"]

```

---

## Database: `./custom_indicators.db`

### Table: `custom_indicators` (0 rows)
```sql
CREATE TABLE custom_indicators (
                    name TEXT PRIMARY KEY,
                    category TEXT,
                    description TEXT,
                    type TEXT,
                    code TEXT,
                    inputs TEXT,
                    outputs TEXT,
                    parameters TEXT
                );
```

---

## File: `./engine.py`

```python
# Use cases:
# - Resolve built-in, third-party, and custom indicators from one standalone engine.
# - Keep strategy code similar to Backtrader while remaining broker-independent.
# - Provide a single indicator integration point for backtests and future live mode.
"""
indicators/engine.py
--------------------
Backtrader-style indicator engine.

This layer provides one place to resolve indicators from:
* the canonical modular pure-Python indicator modules
* the external-library bridge in ``indicators.bridge``
* user-registered custom indicator callables

Strategies can call this engine from ``self.I(...)`` during backtests so
indicator computation stays separate from trading logic.
"""

from __future__ import annotations

import logging
from importlib import import_module
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, List

import pandas as pd
from indicators.bridge import IndicatorBridge

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IndicatorSpec:
    """A single indicator request."""

    indicator: Any
    args: tuple[Any, ...] = field(default_factory=tuple)
    kwargs: Dict[str, Any] = field(default_factory=dict)
    name: Optional[str] = None
    library: str = "auto"


def _make_hashable(val: Any) -> Any:
    """Helper to convert unhashable objects recursively to hashable structures."""
    if isinstance(val, (pd.Series, pd.DataFrame)):
        return id(val)
    if isinstance(val, dict):
        return tuple(sorted((k, _make_hashable(v)) for k, v in val.items()))
    if isinstance(val, (list, tuple, set)):
        return tuple(_make_hashable(item) for item in val)
    try:
        hash(val)
        return val
    except TypeError:
        return id(val)


class IndicatorEngine:
    """
    Resolve and compute indicators from built-ins, bridges, or custom callables.
    """

    def __init__(self, preferred_library: str = "auto", max_cache_size: int = 1000) -> None:
        self.preferred_library = preferred_library
        self._bridge = IndicatorBridge(preferred_library=preferred_library)
        self._custom: Dict[str, Callable[..., Any]] = {}
        self._cache: Dict[tuple, Any] = {}
        self._builtin_module_cache: Dict[str, Optional[Callable]] = {}
        self.max_cache_size = max_cache_size
        self._custom_loaded: bool = False

    def _ensure_custom_loaded(self) -> None:
        if self._custom_loaded:
            return
        self._custom_loaded = True
        try:
            from indicators.custom import CustomIndicatorLoader
            loader = CustomIndicatorLoader()
            loader.load_into_engine(self)
        except Exception as e:
            logger.error("Failed to load custom indicators from SQLite: %s", e)

    def clear_cache(self) -> None:
        """Clear the indicator memoization cache."""
        self._cache.clear()

    def register(self, name: str, func: Callable[..., Any]) -> None:
        """Register a custom indicator callable."""
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Indicator name must not be empty")
        if not callable(func):
            raise TypeError("Indicator function must be callable")
        self._custom[cleaned] = func
        self._bridge.register(cleaned, func)
        logger.info("Registered custom indicator: %s", cleaned)

    def has(self, name: str) -> bool:
        """Return True if the indicator is known to this engine."""
        self._ensure_custom_loaded()
        return name in self._custom or self._has_builtin(name) or hasattr(self._bridge, name)

    def default_name(self, indicator: Any, fallback: str = "indicator") -> str:
        """Derive a reasonable display name for a computed indicator."""
        if isinstance(indicator, str):
            return indicator.strip()
        return getattr(indicator, "__name__", fallback) or fallback

    def resolve(
        self,
        indicator: Any,
        *args: Any,
        name: Optional[str] = None,
        library: str = "auto",
        **kwargs: Any,
    ) -> Any:
        """Alias for compute() to resolve and run an indicator."""
        return self.compute(indicator, *args, name=name, library=library, **kwargs)

    def compute(
        self,
        indicator: Any,
        *args: Any,
        name: Optional[str] = None,
        library: str = "auto",
        **kwargs: Any,
    ) -> Any:
        """
        Compute an indicator from a callable or a string name.

        If a string name is provided, the engine first checks custom
        registrations, then the canonical pure-Python library, and finally the
        bridge.  For bridge-backed indicators, the selected library is passed
        through to support ``pandas_ta`` / ``talib`` selection.
        """
        # Create a cache key using _make_hashable helper
        cache_key_args = tuple(_make_hashable(arg) for arg in args)
        cache_key_kwargs = tuple(sorted((k, _make_hashable(v)) for k, v in kwargs.items()))
        ind_key = indicator if isinstance(indicator, str) else id(indicator)
        cache_key = (ind_key, cache_key_args, cache_key_kwargs, library)

        if cache_key in self._cache:
            return self._cache[cache_key]

        fn = self._resolve(indicator, library=library)
        result = fn(*args, **kwargs)

        # LRU eviction
        if len(self._cache) >= self.max_cache_size:
            self._cache.pop(next(iter(self._cache)))

        self._cache[cache_key] = result
        return result

    def batch_compute(self, specs: List[IndicatorSpec]) -> Dict[str, Any]:
        """Compute multiple indicators efficiently, returning a dict of results."""
        results = {}
        for spec in specs:
            res = self.compute(
                spec.indicator,
                *spec.args,
                name=spec.name,
                library=spec.library,
                **spec.kwargs
            )
            # Use provided name or default derived name
            final_name = spec.name or self.default_name(spec.indicator)
            results[final_name] = res
        return results

    def spec(
        self,
        indicator: Any,
        *args: Any,
        name: Optional[str] = None,
        library: str = "auto",
        **kwargs: Any,
    ) -> IndicatorSpec:
        """Create a reusable indicator request object."""
        return IndicatorSpec(
            indicator=indicator,
            args=tuple(args),
            kwargs=dict(kwargs),
            name=name or self.default_name(indicator),
            library=library,
        )

    def get_registry_schema(self) -> List[Dict[str, Any]]:
        """Return the JSON schema for all registered indicators."""
        from indicators.registry import IndicatorRegistry
        return IndicatorRegistry.to_json_schema()

    def _resolve(self, indicator: Any, library: str = "auto") -> Callable[..., Any]:
        """Resolve an indicator into a callable."""
        if callable(indicator):
            return indicator

        if not isinstance(indicator, str):
            raise TypeError(
                f"indicator must be a callable or string, got {type(indicator).__name__}"
            )

        indicator_name = indicator.strip()
        if not indicator_name:
            raise ValueError("indicator name must not be empty")

        self._ensure_custom_loaded()
        builtin = self._builtin_lookup(indicator_name)
        if indicator_name in self._custom:
            return self._custom[indicator_name]

        # By default, prefer canonical built-ins so their function signatures
        # remain the primary contract across strategies and backtests.
        if builtin is not None and library == "auto":
            return builtin

        # Bridge-backed indicators support explicit external library selection.
        if hasattr(self._bridge, indicator_name):
            bridge_method = getattr(self._bridge, indicator_name)

            def _call_bridge(*args: Any, **kwargs: Any) -> Any:
                try:
                    return bridge_method(*args, library=library, **kwargs)
                except TypeError:
                    return bridge_method(*args, **kwargs)

            return _call_bridge

        if builtin is not None:
            return builtin

        raise KeyError(
            f"Unknown indicator '{indicator_name}'. "
            f"Known custom indicators: {sorted(self._custom.keys())}"
        )

    def _has_builtin(self, name: str) -> bool:
        return self._builtin_lookup(name) is not None

    def _builtin_lookup(self, name: str) -> Optional[Callable[..., Any]]:
        """Look up canonical modular pure-Python indicator functions."""
        if name in self._builtin_module_cache:
            return self._builtin_module_cache[name]

        from indicators.registry import IndicatorRegistry
        meta = IndicatorRegistry.get(name)
        if meta is not None and meta.func is not None:
            self._builtin_module_cache[name] = meta.func
            return meta.func

        self._builtin_module_cache[name] = None
        return None


__all__ = ["IndicatorEngine", "IndicatorSpec"]

```

---

## File: `./expression.py`

```python
"""
indicators/expression.py
------------------------
Safe AST-based evaluator for indicator expressions.

Allows users to write formulaic rules like:
    "sma(close, 50) > sma(close, 200)"
    "rsi(close, 14) < 30 & macd(close)['histogram'] > 0"
    "close > close[-1]"  # shift syntax for previous bar
"""

import ast
import operator as op
import re
from typing import Any, Dict

import numpy as np
import pandas as pd
from indicators.engine import IndicatorEngine
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Set

@dataclass
class CompiledExpression:
    """Compiled representation of an indicator expression."""
    tree: ast.AST
    required_columns: Set[str] = field(default_factory=set)
    required_indicators: Set[str] = field(default_factory=set)

# Supported safe operators
_OPERATORS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Pow: op.pow,
    ast.USub: op.neg,
    ast.Eq: op.eq,
    ast.NotEq: op.ne,
    ast.Lt: op.lt,
    ast.LtE: op.le,
    ast.Gt: op.gt,
    ast.GtE: op.ge,
    ast.BitAnd: op.and_,
    ast.BitOr: op.or_,
    ast.Invert: op.invert,
}


class ExpressionParser:
    """
    Safely parses and evaluates string expressions involving price data
    and indicators using Python's AST.
    """

    def __init__(self, engine: IndicatorEngine, data: Dict[str, pd.Series]) -> None:
        """
        Args:
            engine: Configured IndicatorEngine to compute function calls.
            data: Dictionary of named series (e.g., {"close": df["close"], ...})
        """
        self.engine = engine
        self.data = data

    def evaluate(self, expr: str) -> pd.Series | float | bool:
        """
        Parse and evaluate the expression.

        Replaces 'and' / 'or' with bitwise '&' / '|' prior to parsing
        to support pandas element-wise logical operations.
        
        Raises:
            ValueError: If expression is empty or invalid syntax.
        """
        if not expr or not expr.strip():
            raise ValueError("Expression cannot be empty")

        # Replace logical operators with bitwise ones for pandas
        import re
        # Case-insensitive replacement for 'AND' and 'OR' to 'and' and 'or' so AST can parse them
        expr = re.sub(r'(?i)\band\b', 'and', expr)
        expr = re.sub(r'(?i)\bor\b', 'or', expr)

        compiled = self.parse(expr)
        return self._eval(compiled.tree)

    @classmethod
    @lru_cache(maxsize=128)
    def parse(cls, expr: str) -> CompiledExpression:
        """
        Parse an expression string into a CompiledExpression.
        
        Raises:
            ValueError: If expression is empty or invalid syntax.
        """
        if not expr or not expr.strip():
            raise ValueError("Expression cannot be empty")

        expr = re.sub(r'(?i)\band\b', 'and', expr)
        expr = re.sub(r'(?i)\bor\b', 'or', expr)

        try:
            tree = ast.parse(expr, mode="eval").body
        except SyntaxError as e:
            raise ValueError(f"Invalid expression syntax: {expr}") from e
            
        required_columns = set()
        required_indicators = set()
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                if node.id.lower() not in ("true", "false"):
                    required_columns.add(node.id)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    required_indicators.add(node.func.id)
                    # Exclude the function name from required columns if it was added
                    required_columns.discard(node.func.id)
                    
        return CompiledExpression(
            tree=tree,
            required_columns=required_columns,
            required_indicators=required_indicators
        )

    @classmethod
    def validate(cls, expr: str) -> tuple[bool, str]:
        """
        Validate an expression string.
        Returns a tuple of (is_valid, error_message).
        """
        try:
            cls.parse(expr)
            return True, ""
        except Exception as e:
            return False, str(e)

    def evaluate_boolean(self, expr: str) -> pd.Series:
        """
        Evaluate the expression and ensure the result is a boolean pandas Series.
        Useful for screener rules.
        
        Raises:
            TypeError: If result cannot be converted to a boolean Series.
        """
        result = self.evaluate(expr)
        if isinstance(result, pd.Series):
            return result.astype(bool)
        elif isinstance(result, (bool, np.bool_)):
            # If the expression evaluated to a scalar bool, broadcast to a series based on data index
            if self.data:
                first_series = next(iter(self.data.values()))
                return pd.Series(result, index=first_series.index)
            return pd.Series([result])
        elif isinstance(result, (int, float)):
            if self.data:
                first_series = next(iter(self.data.values()))
                return pd.Series(bool(result), index=first_series.index)
            return pd.Series([bool(result)])
        else:
            raise TypeError(f"Expression result cannot be converted to boolean Series: {type(result)}")

    def _eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value

        elif isinstance(node, ast.Name):
            if node.id in self.data:
                return self.data[node.id]
            # Try to resolve as a boolean constant if lowercased
            if node.id.lower() == "true": return True
            if node.id.lower() == "false": return False
            raise NameError(f"Unknown variable or data series: '{node.id}'")

        elif isinstance(node, ast.BinOp):
            left = self._eval(node.left)
            right = self._eval(node.right)
            op_func = _OPERATORS.get(type(node.op))
            if not op_func:
                raise TypeError(f"Unsupported binary operator: {type(node.op).__name__}")
            return op_func(left, right)

        elif isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand)
            op_func = _OPERATORS.get(type(node.op))
            if not op_func:
                raise TypeError(f"Unsupported unary operator: {type(node.op).__name__}")
            return op_func(operand)

        elif isinstance(node, ast.BoolOp):
            # Evaluate all values and reduce them with bitwise operators for pandas Series
            op_func = op.and_ if isinstance(node.op, ast.And) else op.or_
            result = self._eval(node.values[0])
            for next_node in node.values[1:]:
                result = op_func(result, self._eval(next_node))
            return result

        elif isinstance(node, ast.Compare):
            left = self._eval(node.left)
            if len(node.ops) > 1:
                raise ValueError("Chained comparisons (e.g. 10 < x < 20) are not supported. Use '&' instead.")
            right = self._eval(node.comparators[0])
            op_func = _OPERATORS.get(type(node.ops[0]))
            if not op_func:
                raise TypeError(f"Unsupported comparison operator: {type(node.ops[0]).__name__}")
            return op_func(left, right)

        elif isinstance(node, ast.Call):
            args = [self._eval(arg) for arg in node.args]
            kwargs = {kw.arg: self._eval(kw.value) for kw in node.keywords if kw.arg}

            if isinstance(node.func, ast.Name):
                func_name = node.func.id
                # If it's a known data column/series but called like a func, that's an error
                if func_name in self.data:
                    raise ValueError(f"'{func_name}' is a data series, not a function.")
                return self.engine.compute(func_name, *args, **kwargs)
            elif isinstance(node.func, ast.Attribute):
                func_callable = self._eval(node.func)
                if callable(func_callable):
                    return func_callable(*args, **kwargs)
                raise TypeError("Attribute is not callable")
            else:
                raise ValueError("Only direct function calls and method attributes are supported")

        elif isinstance(node, ast.Subscript):
            value = self._eval(node.value)
            
            # Extract slice value based on AST version (Python 3.8 vs 3.9+)
            if isinstance(node.slice, ast.Constant):
                slice_val = node.slice.value
            elif isinstance(node.slice, ast.Index):  # Python 3.8 fallback
                if isinstance(node.slice.value, ast.Constant):
                    slice_val = node.slice.value.value
                elif isinstance(node.slice.value, ast.UnaryOp) and isinstance(node.slice.value.op, ast.USub):
                    # Negative index
                    slice_val = -node.slice.value.operand.value
                else:
                    slice_val = self._eval(node.slice.value)
            elif isinstance(node.slice, ast.UnaryOp) and isinstance(node.slice.op, ast.USub):
                 slice_val = -self._eval(node.slice.operand)
            else:
                slice_val = self._eval(node.slice)

            # Support shift syntax for pandas Series (e.g. close[-1] -> close.shift(1))
            if isinstance(value, pd.Series) and isinstance(slice_val, int):
                if slice_val < 0:
                    return value.shift(abs(slice_val))
                elif slice_val > 0:
                    return value.shift(-slice_val)
                return value

            # Dictionary access (e.g., macd['histogram'])
            if isinstance(value, (dict, pd.DataFrame)) and isinstance(slice_val, str):
                return value[slice_val]

            raise TypeError(f"Unsupported subscript type {type(slice_val).__name__} on {type(value).__name__}")

        elif isinstance(node, ast.Attribute):
            value = self._eval(node.value)
            if isinstance(value, pd.Series):
                if node.attr == "shift":
                    # We return a callable that applies shift, to be used in ast.Call
                    return value.shift
            raise TypeError(f"Unsupported attribute '{node.attr}' on {type(value).__name__}")

        else:
            raise TypeError(f"Unsupported syntax construct: {type(node).__name__}")

__all__ = ["ExpressionParser"]

```

---

## File: `./helpers.py`

```python
"""
indicators/helpers.py
---------------------
Signal and candlestick helper utilities.

Note on Caching: These functions do not use @functools.lru_cache directly.
Memoization/caching is handled exclusively by the IndicatorEngine to
prevent memory leaks on large live-streaming datasets.
"""

from __future__ import annotations

import pandas as pd


def crossover(series1: pd.Series, series2: pd.Series) -> pd.Series:
    """True on the bar where series1 crosses above series2."""
    valid = series1.notna() & series2.notna() & series1.shift(1).notna() & series2.shift(1).notna()
    return (series1 > series2) & (series1.shift(1) <= series2.shift(1)) & valid


def crossunder(series1: pd.Series, series2: pd.Series) -> pd.Series:
    """True on the bar where series1 crosses below series2."""
    valid = series1.notna() & series2.notna() & series1.shift(1).notna() & series2.shift(1).notna()
    return (series1 < series2) & (series1.shift(1) >= series2.shift(1)) & valid


def above_threshold(series: pd.Series, level: float) -> pd.Series:
    """True on the bar where series crosses above a scalar threshold."""
    valid = series.notna() & series.shift(1).notna()
    return (series > level) & (series.shift(1) <= level) & valid


def below_threshold(series: pd.Series, level: float) -> pd.Series:
    """True on the bar where series crosses below a scalar threshold."""
    valid = series.notna() & series.shift(1).notna()
    return (series < level) & (series.shift(1) >= level) & valid


def candle_body(open_: pd.Series, close: pd.Series) -> pd.Series:
    """Absolute candle body size."""
    return (close - open_).abs()


def candle_range(high: pd.Series, low: pd.Series) -> pd.Series:
    """Full candle range."""
    return high - low


def is_green(open_: pd.Series, close: pd.Series) -> pd.Series:
    """True for bullish candles."""
    return close > open_


def is_red(open_: pd.Series, close: pd.Series) -> pd.Series:
    """True for bearish candles."""
    return close < open_


def upper_shadow(open_: pd.Series, high: pd.Series, close: pd.Series) -> pd.Series:
    """Upper wick length."""
    return high - pd.concat([open_, close], axis=1).max(axis=1)


def lower_shadow(open_: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Lower wick length."""
    return pd.concat([open_, close], axis=1).min(axis=1) - low


__all__ = [
    "crossover",
    "crossunder",
    "above_threshold",
    "below_threshold",
    "candle_body",
    "candle_range",
    "is_green",
    "is_red",
    "upper_shadow",
    "lower_shadow",
]

```

---

## File: `./moving_averages.py`

```python
"""
indicators/moving_averages.py
------------------------------
Pure functions for moving average indicators.
All functions take a pandas Series and return a pandas Series.
No state, no side effects — safe to use in both backtester and live bot.

Note on Caching: These functions do not use @functools.lru_cache directly.
Memoization/caching is handled exclusively by the IndicatorEngine to
prevent memory leaks on large live-streaming datasets.

INDICATORS:
    sma(series, period)            Simple Moving Average
    ema(series, period)            Exponential Moving Average
    dema(series, period)           Double EMA (reduces lag vs EMA)
    wma(series, period)            Weighted Moving Average
    vwap(high, low, close, volume) Volume Weighted Avg Price (intraday)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from indicators.registry import register_indicator

__all__ = ["sma", "ema", "dema", "wma", "vwap"]


@register_indicator(
    name="sma",
    category="TREND",
    inputs=["close"],
    outputs=["sma"],
    parameters={"period": 20},
    display_name="Simple Moving Average",
    output_type="series",
    libraries=["built_in", "talib", "pandas_ta"],
    description="Simple Moving Average",
    example="sma(close, 20)"
)
def sma(series: pd.Series, period: int) -> pd.Series:
    """
    Simple Moving Average.

    Arithmetic mean of the last `period` values. Equal weight to all bars.
    First (period-1) values will be NaN — this is correct behaviour.

    Args:
        series (pd.Series): Price series (typically close prices).
        period (int):       Lookback window. Must be >= 1.

    Returns:
        pd.Series: SMA values, same index as input.

    Example:
        sma_20 = sma(df["close"], 20)
    """
    if series.empty:
        return pd.Series(dtype=float, index=series.index)
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if period > len(series):
        return pd.Series(np.nan, index=series.index)
        
    return series.rolling(window=period, min_periods=period).mean()


@register_indicator(
    name="ema",
    category="TREND",
    inputs=["close"],
    outputs=["ema"],
    parameters={"period": 20},
    display_name="Exponential Moving Average",
    output_type="series",
    libraries=["built_in", "talib", "pandas_ta"],
    description="Exponential Moving Average",
    example="ema(close, 20)"
)
def ema(series: pd.Series, period: int, adjust: bool = False) -> pd.Series:
    """
    Exponential Moving Average.

    Gives more weight to recent prices. The smoothing factor (alpha) is
    2 / (period + 1). Recent values have greater influence than older ones.

    WHY adjust=False: This matches the industry-standard EMA calculation
    used by TradingView, Bloomberg, and NSE. adjust=True uses a different
    formula that produces different values for early data points.

    Args:
        series (pd.Series): Price series.
        period (int):       EMA span. Must be >= 1.
        adjust (bool):      Use pandas EWM adjust parameter. Default False.

    Returns:
        pd.Series: EMA values, same index as input.

    Example:
        ema_50 = ema(df["close"], 50)
    """
    if series.empty:
        return pd.Series(dtype=float, index=series.index)
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if period > len(series):
        return pd.Series(np.nan, index=series.index)
        
    return series.ewm(span=period, adjust=adjust, min_periods=period).mean()


@register_indicator(
    name="dema",
    category="TREND",
    inputs=["close"],
    outputs=["dema"],
    parameters={"period": 20},
    display_name="Double Exponential Moving Average",
    output_type="series",
    libraries=["built_in", "talib"],
    description="Double Exponential Moving Average",
    example="dema(close, 20)"
)
def dema(series: pd.Series, period: int) -> pd.Series:
    """
    Double Exponential Moving Average.

    Formula: DEMA = 2 * EMA(n) - EMA(EMA(n))
    Reduces the lag of a standard EMA by applying EMA twice and correcting.

    WHY DEMA: For trend-following strategies, DEMA reacts faster to price
    changes than EMA while producing fewer whipsaws than simply halving
    the EMA period.

    Args:
        series (pd.Series): Price series.
        period (int):       EMA period used internally.

    Returns:
        pd.Series: DEMA values, same index as input.

    Example:
        dema_20 = dema(df["close"], 20)
    """
    if series.empty:
        return pd.Series(dtype=float, index=series.index)
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if period > len(series):
        return pd.Series(np.nan, index=series.index)
        
    ema1 = ema(series, period)
    ema2 = ema(ema1, period)
    return 2 * ema1 - ema2


@register_indicator(
    name="wma",
    category="TREND",
    inputs=["close"],
    outputs=["wma"],
    parameters={"period": 20},
    display_name="Weighted Moving Average",
    output_type="series",
    libraries=["built_in", "talib"],
    description="Weighted Moving Average",
    example="wma(close, 20)"
)
def wma(series: pd.Series, period: int) -> pd.Series:
    """
    Weighted Moving Average.

    Assigns linearly increasing weights: the most recent bar gets weight
    `period`, second-most-recent gets `period-1`, ..., oldest gets 1.
    More responsive than SMA, less than EMA.

    Args:
        series (pd.Series): Price series.
        period (int):       Lookback window. Must be >= 1.

    Returns:
        pd.Series: WMA values, same index as input.

    Edge case: Returns NaN for the first (period-1) values (insufficient data).

    Example:
        wma_10 = wma(df["close"], 10)
    """
    if series.empty:
        return pd.Series(dtype=float, index=series.index)
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if period > len(series):
        return pd.Series(np.nan, index=series.index)

    weights = np.arange(1, period + 1, dtype=float)  # [1, 2, ..., period]

    def _weighted_mean(window):
        if len(window) < period:
            return np.nan
        return np.dot(window, weights) / weights.sum()

    return series.rolling(window=period, min_periods=period).apply(
        _weighted_mean, raw=True
    )


def vwap(
    high:   pd.Series,
    low:    pd.Series,
    close:  pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """
    Volume Weighted Average Price (VWAP).

    VWAP = Cumulative(Typical Price × Volume) / Cumulative(Volume)
    Typical Price = (High + Low + Close) / 3

    IMPORTANT: VWAP resets every trading day. This implementation computes
    a running intraday VWAP that resets at each new date in the index.
    For daily/weekly/monthly data, VWAP is computed over the entire series
    (no intraday reset).

    WHY VWAP: Institutional traders use VWAP as a benchmark. Price above
    VWAP = bullish bias; price below VWAP = bearish bias.

    Args:
        high   (pd.Series): High prices.
        low    (pd.Series): Low prices.
        close  (pd.Series): Close prices.
        volume (pd.Series): Volume.

    Returns:
        pd.Series: VWAP values, same index as input.

    Edge case: If volume is 0 for an entire day, VWAP for that day = NaN.

    Example:
        df["vwap"] = vwap(df["high"], df["low"], df["close"], df["volume"])
    """
    if close.empty:
        return pd.Series(dtype=float, index=close.index)
        
    typical_price = (high + low + close) / 3
    tp_vol = typical_price * volume

    # Detect if this is intraday data (index has time component)
    has_time = hasattr(close.index, 'date')

    if has_time:
        # Reset cumulative sum at each new date
        date_col = pd.Series(close.index.date, index=close.index)
        cum_tp_vol = tp_vol.groupby(date_col).cumsum()
        cum_vol    = volume.groupby(date_col).cumsum()
    else:
        cum_tp_vol = tp_vol.cumsum()
        cum_vol    = volume.cumsum()

    return cum_tp_vol / cum_vol.replace(0, np.nan)

```

---

## File: `./oscillators.py`

```python
"""
indicators/oscillators.py
--------------------------
Momentum and oscillator indicators.
All functions take pandas Series/DataFrame and return pandas Series.

Note on Caching: These functions do not use @functools.lru_cache directly.
Memoization/caching is handled exclusively by the IndicatorEngine to
prevent memory leaks on large live-streaming datasets.

INDICATORS:
    rsi(series, period)                        Relative Strength Index
    stochastic(high, low, close, k, d)         Stochastic %K and %D
    macd(series, fast, slow, signal)           MACD line, signal, histogram
    roc(series, period)                        Rate of Change (momentum %)
    cci(high, low, close, period)              Commodity Channel Index
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from indicators.moving_averages import ema, sma
from indicators.registry import register_indicator

__all__ = ["rsi", "stochastic", "macd", "roc", "cci"]


@register_indicator(
    name="rsi",
    category="MOMENTUM",
    inputs=["close"],
    outputs=["rsi"],
    parameters={"period": 14},
    display_name="Relative Strength Index",
    output_type="series",
    libraries=["built_in", "talib", "pandas_ta"],
    description="Relative Strength Index",
    example="rsi(close, 14)"
)
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (Wilder's RSI).

    RSI measures the speed and magnitude of price changes.
    Range: 0 to 100.
    Convention: > 70 = overbought, < 30 = oversold.

    IMPLEMENTATION NOTE:
    Wilder's original smoothing uses a modified EMA with alpha = 1/period,
    NOT the standard 2/(period+1) alpha. This is computed via
    ewm(alpha=1/period, adjust=False) — matching TradingView's RSI.

    Args:
        series (pd.Series): Price series (typically close).
        period (int):       Lookback period. Default 14 (Wilder's original).

    Returns:
        pd.Series: RSI values 0–100, NaN for first `period` rows.

    Edge cases:
        - If all gains are 0 for a period → RSI = 0.
        - If all losses are 0 for a period → RSI = 100.
        - Empty series → returns empty Series.

    Example:
        rsi_14 = rsi(df["close"], 14)
        overbought = rsi_14 > 70
        oversold   = rsi_14 < 30
    """
    if series.empty:
        return pd.Series(dtype=float)
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    delta = series.diff()

    gain = delta.clip(lower=0)   # positive changes only
    loss = (-delta).clip(lower=0)  # absolute value of negative changes

    # Wilder's smoothing: alpha = 1/period (NOT 2/(period+1))
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)  # avoid division by zero
    rsi_val = 100 - (100 / (1 + rs))

    # When avg_loss is 0 (all gains), RSI should be 100
    rsi_val[avg_loss == 0] = 100.0
    # When avg_gain is 0 (all losses), RSI should be 0
    rsi_val[avg_gain == 0] = 0.0

    return rsi_val


@register_indicator(
    name="stochastic",
    category="MOMENTUM",
    inputs=["high", "low", "close"],
    outputs=["stoch_k", "stoch_d"],
    parameters={"k_period": 14, "d_period": 3, "smooth_k": 3},
    display_name="Stochastic Oscillator",
    output_type="dataframe",
    libraries=["built_in", "talib", "pandas_ta"],
    description="Stochastic Oscillator",
    example="stochastic(high, low, close, 14, 3, 3)"
)
def stochastic(
    high:   pd.Series,
    low:    pd.Series,
    close:  pd.Series,
    k_period: int = 14,
    d_period: int = 3,
    smooth_k: int = 3,
) -> pd.DataFrame:
    """
    Stochastic Oscillator (%K and %D).

    %K shows where the close is relative to the high-low range over k_period.
    %D is a smoothed version of %K (acts as signal line).

    Formula:
        Raw %K = 100 * (Close - Lowest Low) / (Highest High - Lowest Low)
        %K     = SMA(Raw %K, smooth_k)      [smooth_k=1 gives fast stochastic]
        %D     = SMA(%K, d_period)

    Range: 0–100.
    Convention: %K > 80 = overbought, %K < 20 = oversold.

    Args:
        high     : High price series.
        low      : Low price series.
        close    : Close price series.
        k_period : Lookback for raw %K. Default 14.
        d_period : Smoothing for %D. Default 3.
        smooth_k : Smoothing for %K (3 = slow stochastic). Default 3.

    Returns:
        pd.DataFrame with columns ["stoch_k", "stoch_d"].

    Edge cases:
        - If high == low for all bars in window → %K = NaN (no range to measure).

    Example:
        stoch = stochastic(df["high"], df["low"], df["close"])
        buy_signal = (stoch["stoch_k"] < 20) & (stoch["stoch_k"] > stoch["stoch_d"])
    """
    if close.empty:
        return pd.DataFrame(columns=["stoch_k", "stoch_d"], index=close.index, dtype=float)

    lowest_low   = low.rolling(window=k_period, min_periods=k_period).min()
    highest_high = high.rolling(window=k_period, min_periods=k_period).max()

    price_range = highest_high - lowest_low
    raw_k = 100 * (close - lowest_low) / price_range.replace(0, np.nan)

    k = sma(raw_k, smooth_k) if smooth_k > 1 else raw_k
    d = sma(k, d_period)

    return pd.DataFrame({"stoch_k": k, "stoch_d": d}, index=close.index)


@register_indicator(
    name="macd",
    category="MOMENTUM",
    inputs=["close"],
    outputs=["macd", "signal", "histogram"],
    parameters={"fast_period": 12, "slow_period": 26, "signal_period": 9},
    display_name="Moving Average Convergence Divergence",
    output_type="dataframe",
    libraries=["built_in", "talib", "pandas_ta"],
    description="Moving Average Convergence Divergence",
    example="macd(close, 12, 26, 9)"
)
def macd(
    series:        pd.Series,
    fast_period:   int = 12,
    slow_period:   int = 26,
    signal_period: int = 9,
) -> pd.DataFrame:
    """
    Moving Average Convergence Divergence (MACD).

    MACD Line     = EMA(fast) - EMA(slow)
    Signal Line   = EMA(MACD Line, signal_period)
    Histogram     = MACD Line - Signal Line

    Convention:
        MACD crosses above Signal → bullish momentum building.
        Histogram turns positive  → trend strengthening.
        Histogram turns negative  → trend weakening.

    Args:
        series       : Price series (typically close).
        fast_period  : Short EMA period. Default 12.
        slow_period  : Long EMA period. Default 26.
        signal_period: Signal line EMA period. Default 9.

    Returns:
        pd.DataFrame with columns ["macd", "signal", "histogram"].

    Edge cases:
        - fast_period must be < slow_period.
        - NaN values for first (slow_period + signal_period - 1) bars.

    Example:
        m = macd(df["close"])
        crossover_up = (m["macd"] > m["signal"]) & (m["macd"].shift(1) <= m["signal"].shift(1))
    """
    if series.empty:
        return pd.DataFrame(columns=["macd", "signal", "histogram"], index=series.index, dtype=float)

    if fast_period >= slow_period:
        raise ValueError(
            f"fast_period ({fast_period}) must be less than "
            f"slow_period ({slow_period})"
        )

    fast_ema   = ema(series, fast_period)
    slow_ema   = ema(series, slow_period)
    macd_line  = fast_ema - slow_ema
    signal_line = ema(macd_line, signal_period)
    histogram  = macd_line - signal_line

    return pd.DataFrame({
        "macd":      macd_line,
        "histogram": histogram,
        "signal":    signal_line,
    }, index=series.index)


@register_indicator(
    name="roc",
    category="MOMENTUM",
    inputs=["close"],
    outputs=["roc"],
    parameters={"period": 12},
    display_name="Rate of Change",
    output_type="series",
    libraries=["built_in", "talib", "pandas_ta"],
    description="Rate of Change",
    example="roc(close, 12)"
)
def roc(series: pd.Series, period: int = 12) -> pd.Series:
    """
    Rate of Change — momentum as percentage.

    ROC = 100 * (Close - Close[period ago]) / Close[period ago]

    Positive ROC = price has risen relative to `period` bars ago.
    ROC crossing zero from below = bullish signal.

    Args:
        series (pd.Series): Price series.
        period (int):       Lookback bars. Default 12.

    Returns:
        pd.Series: ROC percentage values.

    Example:
        roc_12 = roc(df["close"], 12)
    """
    if series.empty:
        return pd.Series(dtype=float, index=series.index)
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    return series.pct_change(periods=period) * 100


@register_indicator(
    name="cci",
    category="MOMENTUM",
    inputs=["high", "low", "close"],
    outputs=["cci"],
    parameters={"period": 20},
    display_name="Commodity Channel Index",
    output_type="series",
    libraries=["built_in", "talib", "pandas_ta"],
    description="Commodity Channel Index",
    example="cci(high, low, close, 20)"
)
def cci(
    high:   pd.Series,
    low:    pd.Series,
    close:  pd.Series,
    period: int = 20,
) -> pd.Series:
    """
    Commodity Channel Index.

    CCI = (Typical Price - SMA(Typical Price)) / (0.015 * Mean Deviation)

    Range: Unbounded, but typically oscillates ±100 to ±200.
    Convention: > +100 = overbought, < -100 = oversold.

    The constant 0.015 ensures ~75% of values fall within ±100.

    Args:
        high   : High price series.
        low    : Low price series.
        close  : Close price series.
        period : Lookback window. Default 20.

    Returns:
        pd.Series: CCI values.

    Example:
        cci_20 = cci(df["high"], df["low"], df["close"], 20)
    """
    if close.empty:
        return pd.Series(dtype=float, index=close.index)

    typical_price = (high + low + close) / 3
    tp_sma = sma(typical_price, period)

    # Mean absolute deviation
    mean_dev = typical_price.rolling(window=period).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True
    )

    return (typical_price - tp_sma) / (0.015 * mean_dev.replace(0, np.nan))

```

---

## File: `./registry.py`

```python
"""
indicators/registry.py
----------------------
Registry for indicator metadata (inputs, parameters, outputs).
Used by the Engine to know what an indicator needs and what it returns,
which is critical for the UI, Screener, and ExpressionParser.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
import logging
from typing import Callable, Dict, List, Any, Optional

logger = logging.getLogger(__name__)

class Categories(str, Enum):
    TREND = "TREND"
    MOMENTUM = "MOMENTUM"
    VOLATILITY = "VOLATILITY"
    VOLUME = "VOLUME"
    PATTERN = "PATTERN"
    SUPPORT_RESISTANCE = "SUPPORT_RESISTANCE"
    STATISTICS = "STATISTICS"
    CUSTOM = "CUSTOM"

@dataclass
class IndicatorMeta:
    """Metadata describing an indicator's signature and outputs."""
    name: str
    category: str
    description: str
    inputs: List[str]  # e.g., ["close"], ["high", "low", "close"]
    parameters: Dict[str, Any]  # e.g., {"period": 14, "std_dev": 2.0}
    outputs: List[str]  # e.g., ["RSI_14"], ["macd", "signal", "histogram"]
    display_name: str = ""
    output_type: str = "series"  # "series" | "dataframe" | "scalar" | "list"
    libraries: List[str] = field(default_factory=list)
    example: str = ""
    func: Optional[Callable] = field(default=None, repr=False)

    def __post_init__(self):
        if not self.display_name:
            self.display_name = self.name.replace("_", " ").title()
        if not self.libraries:
            self.libraries = ["built_in"]


class IndicatorRegistry:
    """Thread-safe registry for indicator metadata."""
    _registry: Dict[str, IndicatorMeta] = {}
    _lock = threading.RLock()
    _core_registered: bool = False
    _external_registered: bool = False

    @classmethod
    def register(cls, meta: IndicatorMeta) -> None:
        """Register indicator metadata."""
        with cls._lock:
            cls._registry[meta.name] = meta

    @classmethod
    def get(cls, name: str) -> Optional[IndicatorMeta]:
        """Get indicator metadata by name."""
        cls.auto_register_core()
        cls.auto_register_external()
        with cls._lock:
            return cls._registry.get(name)

    @classmethod
    def list_all(cls) -> Dict[str, IndicatorMeta]:
        """Return a copy of all registered indicators."""
        cls.auto_register_core()
        cls.auto_register_external()
        with cls._lock:
            return cls._registry.copy()

    @classmethod
    def auto_register_core(cls) -> None:
        """
        Eagerly import core indicator modules to run their decorators.
        """
        with cls._lock:
            if cls._core_registered:
                return
            import sys
            import importlib
            for module_name in [
                "indicators.moving_averages",
                "indicators.oscillators",
                "indicators.volatility",
                "indicators.trend",
                "indicators.volume",
                "indicators.statistics",
                "indicators.patterns.candlestick",
                "indicators.patterns.dow_patterns",
            ]:
                try:
                    if module_name not in sys.modules:
                        importlib.import_module(module_name)
                except Exception as e:
                    logger.error("Failed to auto-register %s: %s", module_name, e)
            cls._core_registered = True


    @classmethod
    def auto_register_external(cls) -> None:
        """Auto-register TA-Lib and pandas-ta functions if available."""
        with cls._lock:
            if cls._external_registered:
                return
            try:
                import talib
                for func_name in talib.get_functions():
                    if func_name not in cls._registry:
                        cls.register(IndicatorMeta(
                            name=func_name,
                            category=Categories.CUSTOM.value,
                            description=f"TA-Lib {func_name}",
                            inputs=["*"],
                            parameters={},
                            outputs=[func_name]
                        ))
            except ImportError:
                pass

            try:
                import pandas_ta as ta
                # simple heuristics for pandas_ta functions
                for func_name in dir(ta):
                    if not func_name.startswith("_") and callable(getattr(ta, func_name)) and func_name.islower():
                        if func_name not in cls._registry:
                            cls.register(IndicatorMeta(
                                name=func_name,
                                category=Categories.CUSTOM.value,
                                description=f"pandas-ta {func_name}",
                                inputs=["*"],
                                parameters={},
                                outputs=[func_name]
                            ))
            except ImportError:
                pass

    @classmethod
    def get_by_category(cls, category: str) -> Dict[str, IndicatorMeta]:
        """Return indicators filtered by category."""
        cls.auto_register_core()
        cls.auto_register_external()
        with cls._lock:
            return {k: v for k, v in cls._registry.items() if v.category == category}

    @classmethod
    def search(cls, query: str) -> Dict[str, IndicatorMeta]:
        """Search indicators by name or description."""
        cls.auto_register_core()
        cls.auto_register_external()
        query = query.lower()
        with cls._lock:
            return {
                k: v for k, v in cls._registry.items()
                if query in v.name.lower() or query in v.description.lower()
            }

    @classmethod
    def to_json_schema(cls) -> List[Dict[str, Any]]:
        """Export registry as a list of dictionaries for the UI/Screener."""
        cls.auto_register_core()
        cls.auto_register_external()
        with cls._lock:
            return [
                {
                    "name": v.name,
                    "display_name": v.display_name,
                    "category": v.category,
                    "description": v.description,
                    "inputs": v.inputs,
                    "parameters": v.parameters,
                    "output_type": v.output_type,
                    "outputs": v.outputs,
                    "libraries": v.libraries,
                    "example": v.example,
                }
                for v in cls._registry.values()
            ]


def register_indicator(
    name: str,
    category: str,
    inputs: List[str],
    outputs: List[str],
    parameters: Optional[Dict[str, Any]] = None,
    description: str = "",
    display_name: str = "",
    output_type: str = "series",
    libraries: Optional[List[str]] = None,
    example: str = "",
) -> Callable:
    """
    Decorator to register an indicator function with the registry.
    """
    def decorator(func: Callable) -> Callable:
        disp_name = display_name or name.replace("_", " ").title()
        desc = description or (func.__doc__ or "").strip().split("\n")[0]
        libs = libraries or ["built_in"]
        meta = IndicatorMeta(
            name=name,
            category=category,
            description=desc,
            inputs=inputs,
            parameters=parameters or {},
            outputs=outputs,
            display_name=disp_name,
            output_type=output_type,
            libraries=libs,
            example=example,
            func=func,
        )
        IndicatorRegistry.register(meta)
        return func

    return decorator

__all__ = ["IndicatorMeta", "IndicatorRegistry", "register_indicator", "Categories"]

```

---

## File: `./relative_strength.py`

```python
"""
indicators/relative_strength.py
--------------------------------
Relative Strength (RS) indicator comparing a stock's price momentum
to a benchmark (e.g., NIFTY index).

FORMULA:
    RS(n) = (close_stock[0] / close_stock[-n]) / (close_benchmark[0] / close_benchmark[-n]) - 1

    - RS > 0  → stock is outperforming the benchmark over n periods
    - RS < 0  → stock is underperforming the benchmark
    - RS = 0 is used as a normalised signal

USAGE:
    from indicators.relative_strength import relative_strength

    rs = relative_strength(stock_close, nifty_close, period=55)
    signal = rs > 0   # True when stock outperforms NIFTY over last 55 bars

Note on Caching: These functions do not use @functools.lru_cache directly.
Memoization/caching is handled exclusively by the IndicatorEngine to
prevent memory leaks on large live-streaming datasets.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["relative_strength", "relative_strength_rank"]


def relative_strength(
    stock_close: pd.Series,
    benchmark_close: pd.Series,
    period: int = 55,
) -> pd.Series:
    """
    Compute the Relative Strength of a stock vs a benchmark index.

    RS(n) = (stock[0] / stock[-n]) / (benchmark[0] / benchmark[-n]) - 1

    The result is centred at 0:
      > 0  → stock outperforms benchmark over the lookback
      < 0  → stock underperforms benchmark over the lookback
      = 0  → equal performance

    Parameters
    ----------
    stock_close : pd.Series
        Daily (or any timeframe) closing prices of the stock.
    benchmark_close : pd.Series
        Closing prices of the benchmark index (e.g., NIFTY spot).
        Must be aligned to the same index as stock_close.
    period : int
        Lookback window in bars. Default 55.

    Returns
    -------
    pd.Series
        RS values centred at 0, same index as stock_close.
        First `period` values are NaN (insufficient data).

    Raises
    ------
    ValueError
        If period < 1 or series lengths mismatch.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    # Align both series on the common index
    stock_close, benchmark_close = stock_close.align(benchmark_close, join="inner")

    if stock_close.empty:
        return pd.Series(dtype=float, name=f"RS_{period}")

    # Rolling ratio: current / lagged
    stock_return     = stock_close / stock_close.shift(period)
    benchmark_return = benchmark_close / benchmark_close.shift(period)

    # Avoid division by zero when benchmark return is exactly 0
    rs_raw = stock_return / benchmark_return.replace(0, np.nan)

    # Centre at 0: positive = outperform, negative = underperform
    rs = rs_raw - 1.0
    rs.name = f"RS_{period}"
    return rs


def relative_strength_rank(
    stocks_close: dict,
    benchmark_close: pd.Series,
    period: int = 55,
) -> pd.DataFrame:
    """
    Compute RS for multiple stocks and return a ranked DataFrame.

    Useful for universe-level screening — gives you a cross-sectional
    view of which stocks have the highest RS on each date.

    Parameters
    ----------
    stocks_close : dict
        {symbol: pd.Series} of closing prices.
    benchmark_close : pd.Series
        Benchmark closing prices (NIFTY).
    period : int
        RS lookback period. Default 55.

    Returns
    -------
    pd.DataFrame
        Columns = symbols, index = dates, values = RS scores.
    """
    rs_dict = {}
    for symbol, close in stocks_close.items():
        try:
            rs_dict[symbol] = relative_strength(close, benchmark_close, period)
        except Exception as e:
            print(f"[RS] Skipped {symbol}: {e}")
    return pd.DataFrame(rs_dict)
```

---

## File: `./statistics.py`

```python
"""
indicators/statistics.py
--------------------------
Statistical indicators for mean reversion and pairs trading strategies.

Note on Caching: These functions do not use @functools.lru_cache directly.
Memoization/caching is handled exclusively by the IndicatorEngine to
prevent memory leaks on large live-streaming datasets.

INDICATORS:
    zscore(series, period)                  Rolling Z-score
    rolling_correlation(s1, s2, period)     Rolling Pearson correlation
    rolling_beta(s1, s2, period)            Rolling OLS beta
    spread(s1, s2, hedge_ratio)             Price spread between two instruments
    half_life(spread_series)               Mean reversion half-life (Ornstein-Uhlenbeck)
    cointegration_test(s1, s2)             Engle-Granger cointegration test
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from indicators.registry import register_indicator

__all__ = ["zscore", "rolling_correlation", "rolling_beta", "spread", "half_life", "cointegration_test"]


@register_indicator(
    name="zscore",
    category="STATISTICS",
    inputs=["close"],
    outputs=["zscore"],
    parameters={"period": 20},
    display_name="Rolling Z-Score",
    output_type="series",
    libraries=["built_in"],
    description="Rolling Z-Score",
    example="zscore(close, 20)"
)
def zscore(series: pd.Series, period: int) -> pd.Series:
    """
    Rolling Z-score.

    z = (Price - Rolling Mean) / Rolling Std Dev

    Interpretation:
        z > +2  → Price is 2 standard deviations above mean → overbought
        z < -2  → Price is 2 standard deviations below mean → oversold
        z = 0   → Price is at the rolling mean

    WHY Z-SCORE: The z-score normalises price deviation regardless of the
    absolute price level. Used heavily in mean reversion and pairs trading.

    Args:
        series (pd.Series): Price or spread series.
        period (int):       Rolling window size.

    Returns:
        pd.Series: Z-score values.

    Edge cases:
        - If rolling std = 0 (all prices identical) → NaN (avoid div by zero).
        - First (period-1) values are NaN.

    Example:
        z = zscore(df["close"], 20)
        buy_signal  = z < -2
        sell_signal = z > +2
    """
    if period < 2:
        raise ValueError(f"period must be >= 2 to compute std dev, got {period}")

    roll_mean = series.rolling(window=period, min_periods=period).mean()
    roll_std  = series.rolling(window=period, min_periods=period).std(ddof=1)

    return (series - roll_mean) / roll_std.replace(0, np.nan)


@register_indicator(
    name="rolling_correlation",
    category="STATISTICS",
    inputs=["s1", "s2"],
    outputs=["correlation"],
    parameters={"period": 20},
    display_name="Rolling Correlation",
    output_type="series",
    libraries=["built_in"],
    description="Rolling Correlation",
    example="rolling_correlation(s1, s2, 20)"
)
def rolling_correlation(
    s1:     pd.Series,
    s2:     pd.Series,
    period: int,
) -> pd.Series:
    """
    Rolling Pearson correlation between two price series.

    Range: -1 (perfectly inverse) to +1 (perfectly correlated).

    Used for pairs trading to verify the ongoing correlation between
    two historically correlated instruments.

    Args:
        s1, s2  : Two price series with aligned indices.
        period  : Rolling window.

    Returns:
        pd.Series: Correlation values in [-1, 1].

    Edge cases:
        - If either series is constant in the window → NaN.
        - Indices must be aligned; misaligned indices will produce NaN.

    Example:
        corr = rolling_correlation(df["RELIANCE"], df["ONGC"], 60)
    """
    if period < 2:
        raise ValueError(f"period must be >= 2, got {period}")
    return s1.rolling(window=period, min_periods=period).corr(s2)


@register_indicator(
    name="rolling_beta",
    category="STATISTICS",
    inputs=["s1", "s2"],
    outputs=["beta"],
    parameters={"period": 20},
    display_name="Rolling Beta",
    output_type="series",
    libraries=["built_in"],
    description="Rolling Beta",
    example="rolling_beta(s1, s2, 20)"
)
def rolling_beta(
    s1:     pd.Series,
    s2:     pd.Series,
    period: int,
) -> pd.Series:
    """
    Rolling OLS Beta (hedge ratio).

    Beta = Cov(s1, s2) / Var(s2)

    Used in pairs trading to determine how many units of s2 to short
    for every unit of s1 held long (the hedge ratio).

    Args:
        s1     : Dependent series (the instrument you're long).
        s2     : Independent series (the instrument you're short).
        period : Rolling window.

    Returns:
        pd.Series: Rolling beta (hedge ratio) values.

    Example:
        beta = rolling_beta(df["HDFC"], df["ICICIBANK"], 60)
        spread = df["HDFC"] - beta * df["ICICIBANK"]
    """
    if period < 2:
        raise ValueError(f"period must be >= 2, got {period}")

    cov = s1.rolling(window=period, min_periods=period).cov(s2)
    var = s2.rolling(window=period, min_periods=period).var(ddof=1)

    return cov / var.replace(0, np.nan)


@register_indicator(
    name="spread",
    category="STATISTICS",
    inputs=["s1", "s2"],
    outputs=["spread"],
    parameters={"hedge_ratio": 1.0},
    display_name="Spread",
    output_type="series",
    libraries=["built_in"],
    description="Spread",
    example="spread(s1, s2, 1.0)"
)
def spread(
    s1:          pd.Series,
    s2:          pd.Series,
    hedge_ratio: float = 1.0,
) -> pd.Series:
    """
    Compute the price spread between two instruments.

    spread = s1 - hedge_ratio × s2

    When the spread is stationary (cointegrated), it reverts to its mean.
    The hedge_ratio is typically computed via rolling_beta().

    Args:
        s1           : Series 1 (instrument you're long).
        s2           : Series 2 (instrument you're short/hedge).
        hedge_ratio  : Number of s2 units per s1 unit. Default 1.0.

    Returns:
        pd.Series: Spread values.

    Example:
        hr     = rolling_beta(df["HDFC"], df["ICICIBANK"], 60)
        sp     = spread(df["HDFC"], df["ICICIBANK"], hr)
        z      = zscore(sp, 20)
    """
    if hedge_ratio == 0:
        raise ValueError("hedge_ratio cannot be 0")
    return s1 - hedge_ratio * s2


@register_indicator(
    name="half_life",
    category="STATISTICS",
    inputs=["close"],
    outputs=["half_life"],
    parameters={},
    display_name="Half Life",
    output_type="scalar",
    libraries=["built_in"],
    description="Half Life",
    example="half_life(close)"
)
def half_life(spread_series: pd.Series) -> float:
    """
    Estimate the mean reversion half-life using the Ornstein-Uhlenbeck model.

    Half-life = the expected number of bars for the spread to revert
    halfway back to its mean after a deviation.

    Formula:
        Regress: Δspread_t = λ × spread_(t-1) + ε
        half_life = -ln(2) / λ

    Interpretation:
        Short half-life (e.g. 5 bars) → fast mean reversion → better for trading
        Long half-life (> 252 bars)   → very slow reversion → not tradeable

    Args:
        spread_series (pd.Series): The spread or price series to test.

    Returns:
        float: Half-life in number of bars. Returns inf if no mean reversion detected.

    Edge cases:
        - If λ >= 0 (no mean reversion) → returns infinity.
        - Needs at least 30 observations for a reliable estimate.

    Example:
        hl = half_life(sp)
        if hl < 30:
            print("Fast mean-reverting — good for pairs trading")
    """
    if len(spread_series.dropna()) < 30:
        return float("inf")

    spread_lag   = spread_series.shift(1).dropna()
    spread_diff  = spread_series.diff().dropna()

    # Align (diff removes first row, shift removes first row)
    spread_lag, spread_diff = spread_lag.align(spread_diff, join="inner")

    if len(spread_lag) < 10:
        return float("inf")

    # OLS: spread_diff = λ × spread_lag + constant
    # Using numpy polyfit for the regression
    x = spread_lag.values
    y = spread_diff.values

    # Add constant column
    X = np.column_stack([x, np.ones(len(x))])
    try:
        coefs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        lam = coefs[0]
    except np.linalg.LinAlgError:
        return float("inf")

    if lam >= 0:
        return float("inf")   # No mean reversion (λ must be negative)

    return float(-np.log(2) / lam)


@register_indicator(
    name="cointegration_test",
    category="STATISTICS",
    inputs=["s1", "s2"],
    outputs=["is_cointegrated", "p_value", "hedge_ratio", "adf_statistic"],
    parameters={"significance": 0.05},
    display_name="Cointegration Test",
    output_type="dict",
    libraries=["built_in"],
    description="Cointegration Test",
    example="cointegration_test(s1, s2, 0.05)"
)
def cointegration_test(
    s1: pd.Series,
    s2: pd.Series,
    significance: float = 0.05,
) -> dict:
    """
    Engle-Granger two-step cointegration test.

    Tests whether two non-stationary price series share a long-run
    equilibrium relationship (i.e., their spread is stationary).

    Step 1: OLS regression of s1 on s2 to get residuals (the spread).
    Step 2: ADF test on residuals to check for stationarity.

    Args:
        s1, s2        : Two price series (same length, aligned index).
        significance  : p-value threshold. Default 0.05 (95% confidence).

    Returns:
        dict with keys:
            is_cointegrated (bool)   : True if cointegrated at given significance.
            p_value (float)          : ADF test p-value of residuals.
            hedge_ratio (float)      : OLS regression coefficient (β).
            adf_statistic (float)    : ADF test statistic.

    NOTE: This uses statsmodels for the ADF test. If statsmodels is not
    installed, raises ImportError with installation instructions.

    Edge cases:
        - Requires at least 30 aligned observations.
        - Series must have overlapping non-NaN values.

    Example:
        result = cointegration_test(df["HDFC"], df["ICICIBANK"])
        if result["is_cointegrated"]:
            hr     = result["hedge_ratio"]
            sp     = spread(df["HDFC"], df["ICICIBANK"], hr)
    """
    try:
        from statsmodels.tsa.stattools import adfuller
    except ImportError:
        raise ImportError(
            "statsmodels is required for cointegration_test(). "
            "Install it with: pip install statsmodels"
        )

    # Align and drop NaN
    df_aligned = pd.concat([s1, s2], axis=1).dropna()
    if len(df_aligned) < 30:
        raise ValueError(
            f"Need at least 30 observations for cointegration test, "
            f"got {len(df_aligned)}"
        )

    y = df_aligned.iloc[:, 0].values
    x = df_aligned.iloc[:, 1].values

    # Step 1: OLS regression
    X = np.column_stack([x, np.ones(len(x))])
    try:
        coefs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return {
            "is_cointegrated": False,
            "p_value":         1.0,
            "hedge_ratio":     1.0,
            "adf_statistic":   0.0,
        }

    hedge_ratio = coefs[0]
    residuals   = y - X @ coefs

    # Step 2: ADF test on residuals
    adf_result = adfuller(residuals, autolag="AIC")
    adf_stat   = float(adf_result[0])
    p_value    = float(adf_result[1])

    return {
        "is_cointegrated": p_value < significance,
        "p_value":         round(p_value, 6),
        "hedge_ratio":     round(hedge_ratio, 6),
        "adf_statistic":   round(adf_stat, 6),
    }

```

---

## File: `./trend.py`

```python
"""
indicators/trend.py
--------------------
Trend-following indicators.

Note on Caching: These functions do not use @functools.lru_cache directly.
Memoization/caching is handled exclusively by the IndicatorEngine to
prevent memory leaks on large live-streaming datasets.

INDICATORS:
    supertrend(high, low, close, period, multiplier)   Supertrend
    adx(high, low, close, period)                      Average Directional Index
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from indicators.volatility import atr
from indicators.registry import register_indicator

__all__ = ["supertrend", "adx"]


@register_indicator(
    name="supertrend",
    category="TREND",
    inputs=["high", "low", "close"],
    outputs=["supertrend", "direction", "buy_signal", "sell_signal"],
    parameters={"period": 10, "multiplier": 3.0},
    display_name="Supertrend",
    output_type="dataframe",
    libraries=["built_in"],
    description="Supertrend",
    example="supertrend(high, low, close, 10, 3.0)"
)
def supertrend(
    high:       pd.Series,
    low:        pd.Series,
    close:      pd.Series,
    period:     int   = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """
    Supertrend Indicator.

    Supertrend determines trend direction using ATR-based bands around
    the midpoint of the High-Low range.

    Basic Upper Band = (High + Low) / 2 + multiplier × ATR
    Basic Lower Band = (High + Low) / 2 - multiplier × ATR

    The final Supertrend line flips between the upper and lower band
    based on close price position.

    Signal:
        direction = +1 → Uptrend (price above Supertrend line)
        direction = -1 → Downtrend (price below Supertrend line)

    WHY SUPERTREND: Clean binary signal (long/flat). Self-adjusting
    stop-loss. Works well on trending markets. Used widely on NSE equities.

    Args:
        high, low, close : OHLC series.
        period           : ATR period. Default 10.
        multiplier       : ATR multiplier for band width. Default 3.0.

    Returns:
        pd.DataFrame with columns:
            supertrend     : The Supertrend line value (acts as trailing stop)
            direction      : +1 = uptrend, -1 = downtrend
            buy_signal     : True on the bar where trend flips to +1
            sell_signal    : True on the bar where trend flips to -1

    Edge cases:
        - First `period` rows are NaN (ATR not yet available).
        - If close equals the Supertrend exactly → treated as uptrend.

    Example:
        st = supertrend(df["high"], df["low"], df["close"], 10, 3.0)
        buy_entry  = st["buy_signal"]
        sell_entry = st["sell_signal"]
    """
    if close.empty:
        return pd.DataFrame(columns=["supertrend", "direction", "buy_signal", "sell_signal"], index=close.index, dtype=float)

    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if multiplier <= 0:
        raise ValueError(f"multiplier must be > 0, got {multiplier}")

    if len(close) < period:
        # Return dataframe of NaNs since we can't compute ATR
        df = pd.DataFrame(np.nan, index=close.index, columns=["supertrend", "direction", "buy_signal", "sell_signal"])
        df["direction"] = 0
        df["buy_signal"] = False
        df["sell_signal"] = False
        return df

    atr_val  = atr(high, low, close, period)
    hl_mid   = (high + low) / 2

    basic_upper = hl_mid + multiplier * atr_val
    basic_lower = hl_mid - multiplier * atr_val

    # Final bands and direction are computed iteratively — cannot be vectorised
    # because each bar's final band depends on the previous bar's final band.
    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    supertrend_line = pd.Series(np.nan, index=close.index)
    direction       = pd.Series(0,      index=close.index, dtype=int)

    for i in range(1, len(close)):
        if pd.isna(atr_val.iloc[i]):
            continue

        # Final Upper Band:
        # If previous final upper < current basic upper, OR
        # if previous close was > previous final upper → reset to basic
        if basic_upper.iloc[i] < final_upper.iloc[i - 1] or \
                close.iloc[i - 1] > final_upper.iloc[i - 1]:
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i - 1]

        # Final Lower Band:
        if basic_lower.iloc[i] > final_lower.iloc[i - 1] or \
                close.iloc[i - 1] < final_lower.iloc[i - 1]:
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i - 1]

        # Determine direction
        prev_dir = direction.iloc[i - 1]

        if prev_dir == -1 and close.iloc[i] > final_upper.iloc[i]:
            direction.iloc[i] = 1   # flip to uptrend
        elif prev_dir == 1 and close.iloc[i] < final_lower.iloc[i]:
            direction.iloc[i] = -1  # flip to downtrend
        else:
            direction.iloc[i] = prev_dir if prev_dir != 0 else 1

        # Supertrend line = lower band in uptrend, upper band in downtrend
        supertrend_line.iloc[i] = (
            final_lower.iloc[i] if direction.iloc[i] == 1
            else final_upper.iloc[i]
        )

    # Buy signal: direction flips from -1 to +1
    # Sell signal: direction flips from +1 to -1
    buy_signal  = (direction == 1)  & (direction.shift(1) == -1)
    sell_signal = (direction == -1) & (direction.shift(1) == 1)

    return pd.DataFrame({
        "supertrend":  supertrend_line,
        "direction":   direction,
        "buy_signal":  buy_signal,
        "sell_signal": sell_signal,
    }, index=close.index)


@register_indicator(
    name="adx",
    category="TREND",
    inputs=["high", "low", "close"],
    outputs=["adx", "plus_di", "minus_di"],
    parameters={"period": 14},
    display_name="Average Directional Index",
    output_type="dataframe",
    libraries=["built_in", "talib", "pandas_ta"],
    description="Average Directional Index",
    example="adx(high, low, close, 14)"
)
def adx(
    high:   pd.Series,
    low:    pd.Series,
    close:  pd.Series,
    period: int = 14,
) -> pd.DataFrame:
    """
    Average Directional Index (ADX) with +DI and -DI.

    ADX measures TREND STRENGTH (not direction).
    +DI measures upward movement strength.
    -DI measures downward movement strength.

    Interpretation:
        ADX > 25 → Strong trend (worth following)
        ADX < 20 → Weak/no trend (avoid trend strategies)
        +DI > -DI → Uptrend
        -DI > +DI → Downtrend
        +DI crosses above -DI → Buy signal
        -DI crosses above +DI → Sell signal

    Args:
        high, low, close : OHLC series.
        period           : Wilder smoothing period. Default 14.

    Returns:
        pd.DataFrame with columns: adx, plus_di, minus_di.

    Edge cases:
        - First (2 × period) rows are NaN.
        - If True Range is 0 (no price movement) → DI = NaN.

    Example:
        adx_df = adx(df["high"], df["low"], df["close"])
        strong_uptrend = (adx_df["adx"] > 25) & (adx_df["plus_di"] > adx_df["minus_di"])
    """
    if close.empty:
        return pd.DataFrame(columns=["adx", "plus_di", "minus_di"], index=close.index, dtype=float)

    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    # True Range
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Directional movement
    up_move   = high - prev_high
    down_move = prev_low - low

    # +DM: upward move larger and positive
    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm  = pd.Series(plus_dm,  index=close.index)
    minus_dm = pd.Series(minus_dm, index=close.index)

    # Wilder's smoothing for TR, +DM, -DM
    smooth_tr      = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    smooth_plus_dm = plus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    smooth_minus_dm= minus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

    plus_di  = 100 * smooth_plus_dm  / smooth_tr.replace(0, np.nan)
    minus_di = 100 * smooth_minus_dm / smooth_tr.replace(0, np.nan)

    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

    return pd.DataFrame({
        "adx":      adx_val,
        "plus_di":  plus_di,
        "minus_di": minus_di,
    }, index=close.index)

```

---

## File: `./volatility.py`

```python
"""
indicators/volatility.py
--------------------------
Volatility-based indicators.

Note on Caching: These functions do not use @functools.lru_cache directly.
Memoization/caching is handled exclusively by the IndicatorEngine to
prevent memory leaks on large live-streaming datasets.

INDICATORS:
    atr(high, low, close, period)         Average True Range
    bollinger_bands(series, period, std_dev) Bollinger Bands (upper, mid, lower, %B, bandwidth)
    keltner_channels(high,low,close,...)  Keltner Channels
    bb_squeeze(df, ...)                   Bollinger Band / Keltner squeeze detector
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from indicators.moving_averages import ema, sma
from indicators.registry import register_indicator

__all__ = ["atr", "bollinger_bands", "keltner_channels", "bb_squeeze"]


@register_indicator(
    name="atr",
    category="VOLATILITY",
    inputs=["high", "low", "close"],
    outputs=["atr"],
    parameters={"period": 14},
    display_name="Average True Range",
    output_type="series",
    libraries=["built_in", "talib", "pandas_ta"],
    description="Average True Range",
    example="atr(high, low, close, 14)"
)
def atr(
    high:   pd.Series,
    low:    pd.Series,
    close:  pd.Series,
    period: int = 14,
) -> pd.Series:
    """
    Average True Range (Wilder's ATR).

    True Range = max(
        High - Low,
        |High - Previous Close|,
        |Low  - Previous Close|
    )
    ATR = Wilder's EMA(True Range, period)

    WHY ATR: ATR captures volatility regardless of direction. Used for:
    - Position sizing (risk per trade = 1 ATR)
    - Stop-loss placement (e.g. 2× ATR below entry)
    - Supertrend calculation

    Args:
        high   : High prices.
        low    : Low prices.
        close  : Close prices.
        period : Smoothing period. Default 14 (Wilder's original).

    Returns:
        pd.Series: ATR values. First value is NaN (needs previous close).

    Edge cases:
        - Single-bar series → returns NaN.
        - All identical prices → ATR = 0.

    Example:
        atr_14 = atr(df["high"], df["low"], df["close"], 14)
        stop_loss = entry_price - 2 * atr_14.iloc[-1]
    """
    if close.empty:
        return pd.Series(dtype=float, index=close.index)
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Wilder's smoothing: same as RSI smoothing (alpha = 1/period)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# def bollinger_bands(
#     series: pd.Series,
#     period: int   = 20,
#     std_dev: float = 2.0,
# ) -> pd.DataFrame:
@register_indicator(
    name="bollinger_bands",
    category="VOLATILITY",
    inputs=["close"],
    outputs=["bb_lower", "bb_middle", "bb_upper", "bb_bandwidth", "bb_pct_b"],
    parameters={"period": 20, "std_dev": 2.0},
    display_name="Bollinger Bands",
    output_type="dataframe",
    libraries=["built_in", "talib", "pandas_ta"],
    description="Bollinger Bands",
    example="bollinger_bands(close, 20, 2.0)"
)
def bollinger_bands(
    series: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> pd.DataFrame:
    """
    Bollinger Bands.

    Middle Band = SMA(period)
    Upper Band  = Middle Band + std_dev × rolling std deviation
    Lower Band  = Middle Band - std_dev × rolling std deviation
    %B          = (Price - Lower) / (Upper - Lower)   [0=lower, 1=upper, 0.5=middle]
    Bandwidth   = (Upper - Lower) / Middle             [squeeze when very low]

    WHY BOLLINGER BANDS: Price tends to stay within the bands (~95% of time
    with 2σ). Breakouts signal strong moves; contractions signal upcoming
    breakouts.

    Args:
        series  : Price series (typically close).
        period  : SMA period. Default 20.
        std_dev : Number of standard deviations (historically num_std). Default 2.0.

    Returns:
        pd.DataFrame with columns:
            bb_upper, bb_middle, bb_lower, bb_pct_b, bb_bandwidth

    Edge cases:
        - When bandwidth = 0 (all prices identical) → %B = NaN.
        - First (period-1) rows are NaN.

    Example:
        bb = bollinger_bands(df["close"])
        df["bb_upper"] = bb["bb_upper"]
        buy  = df["close"] < bb["bb_lower"]    # price touches lower band
        sell = df["close"] > bb["bb_upper"]    # price touches upper band
    """
    if series.empty:
        return pd.DataFrame(columns=["bb_upper", "bb_middle", "bb_lower", "bb_pct_b", "bb_bandwidth"], index=series.index, dtype=float)

    if period < 2:
        raise ValueError(f"period must be >= 2 for std deviation, got {period}")
    
    if std_dev <= 0:
        raise ValueError(f"std_dev must be > 0, got {std_dev}")

    middle = series.rolling(window=period, min_periods=period).mean()
    std    = series.rolling(window=period, min_periods=period).std(ddof=1)

    upper = middle + std_dev * std
    lower = middle - std_dev * std

    band_width = upper - lower
    pct_b     = (series - lower) / band_width.replace(0, np.nan)
    bandwidth  = band_width / middle.replace(0, np.nan)

    return pd.DataFrame({
        "bb_upper":     upper,
        "bb_middle":    middle,
        "bb_lower":     lower,
        "bb_pct_b":     pct_b,
        "bb_bandwidth": bandwidth,
    }, index=series.index)


@register_indicator(
    name="keltner_channels",
    category="VOLATILITY",
    inputs=["high", "low", "close"],
    outputs=["kc_upper", "kc_middle", "kc_lower"],
    parameters={"ema_period": 20, "atr_period": 10, "multiplier": 2.0},
    display_name="Keltner Channels",
    output_type="dataframe",
    libraries=["built_in", "pandas_ta"],
    description="Keltner Channels",
    example="keltner_channels(high, low, close, 20, 10, 2.0)"
)
def keltner_channels(
    high:       pd.Series,
    low:        pd.Series,
    close:      pd.Series,
    ema_period: int   = 20,
    atr_period: int   = 10,
    multiplier: float = 2.0,
) -> pd.DataFrame:
    """
    Keltner Channels.

    Middle = EMA(close, ema_period)
    Upper  = Middle + multiplier × ATR(atr_period)
    Lower  = Middle - multiplier × ATR(atr_period)

    WHY KELTNER vs BOLLINGER: Keltner uses ATR (true volatility) instead of
    standard deviation. It's smoother and less prone to sudden widening from
    single spike bars.

    Args:
        high       : High prices.
        low        : Low prices.
        close      : Close prices.
        ema_period : EMA period for middle line. Default 20.
        atr_period : ATR period. Default 10.
        multiplier : ATR multiplier. Default 2.0.

    Returns:
        pd.DataFrame with columns: kc_upper, kc_middle, kc_lower.

    Example:
        kc = keltner_channels(df["high"], df["low"], df["close"])
    """
    if close.empty:
        return pd.DataFrame(columns=["kc_upper", "kc_middle", "kc_lower"], index=close.index, dtype=float)

    middle = ema(close, ema_period)
    atr_val = atr(high, low, close, atr_period)

    upper = middle + multiplier * atr_val
    lower = middle - multiplier * atr_val

    return pd.DataFrame({
        "kc_upper":  upper,
        "kc_middle": middle,
        "kc_lower":  lower,
    }, index=close.index)


@register_indicator(
    name="bb_squeeze",
    category="VOLATILITY",
    inputs=["high", "low", "close"],
    outputs=["bb_squeeze"],
    parameters={"bb_period": 20, "bb_std": 2.0, "kc_ema": 20, "kc_atr": 10, "kc_mult": 1.5},
    display_name="Bollinger Band Keltner Squeeze",
    output_type="series",
    libraries=["built_in"],
    description="Bollinger Band Keltner Squeeze",
    example="bb_squeeze(high, low, close)"
)
def bb_squeeze(
    high:        pd.Series,
    low:         pd.Series,
    close:       pd.Series,
    bb_period:   int   = 20,
    bb_std:      float = 2.0,
    kc_ema:      int   = 20,
    kc_atr:      int   = 10,
    kc_mult:     float = 1.5,
) -> pd.Series:
    """
    Bollinger Band / Keltner Channel Squeeze detector.

    A "squeeze" occurs when Bollinger Bands are INSIDE Keltner Channels,
    indicating low volatility compression before an explosive move.

    Returns True (squeeze on) when BB is inside KC, False when released.

    WHY SQUEEZE: After a period of compression, price tends to break out
    with higher-than-average momentum. The squeeze release is a high-
    probability entry signal.

    Args:
        high, low, close : OHLC series.
        bb_period        : Bollinger Bands period.
        bb_std           : Bollinger Bands std dev multiplier.
        kc_ema           : Keltner EMA period.
        kc_atr           : Keltner ATR period.
        kc_mult          : Keltner multiplier (lower = tighter KC = fewer squeezes).

    Returns:
        pd.Series of bool: True = squeeze active, False = squeeze released.

    Example:
        squeeze = bb_squeeze(df["high"], df["low"], df["close"])
        squeeze_released = squeeze.shift(1) & ~squeeze   # squeeze just ended
    """
    if close.empty:
        return pd.Series(dtype=bool, index=close.index)

    bb = bollinger_bands(close, bb_period, bb_std)
    kc = keltner_channels(high, low, close, kc_ema, kc_atr, kc_mult)

    # Squeeze = BB upper is below KC upper AND BB lower is above KC lower
    squeeze_on = (bb["bb_upper"] < kc["kc_upper"]) & \
                 (bb["bb_lower"] > kc["kc_lower"])

    return squeeze_on

```

---

## File: `./volume.py`

```python
"""
indicators/volume.py
--------------------
Volume-based indicators.

Note on Caching: These functions do not use @functools.lru_cache directly.
Memoization/caching is handled exclusively by the IndicatorEngine to
prevent memory leaks on large live-streaming datasets.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from indicators.registry import register_indicator


@register_indicator(
    name="vwap",
    category="VOLUME",
    inputs=["high", "low", "close", "volume"],
    outputs=["vwap"],
    parameters={},
    display_name="Volume Weighted Average Price",
    output_type="series",
    libraries=["built_in", "pandas_ta"],
    description="Volume Weighted Average Price",
    example="vwap(high, low, close, volume)"
)
def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """
    Volume Weighted Average Price.

    For intraday data with a DatetimeIndex, VWAP resets each trading day.
    For non-datetime indices, it is computed across the full series.

    NOTE ON TIMEZONES: Ensure the DatetimeIndex is timezone-aware (e.g. IST)
    before calling vwap, so that `index.date` correctly identifies market days.
    """
    if close.empty:
        return pd.Series(dtype=float, index=close.index, name="VWAP")

    typical_price = (high + low + close) / 3
    price_volume = typical_price * volume

    if isinstance(close.index, pd.DatetimeIndex):
        session_key = pd.Series(close.index.date, index=close.index)
        cumulative_pv = price_volume.groupby(session_key).cumsum()
        cumulative_volume = volume.groupby(session_key).cumsum()
    else:
        cumulative_pv = price_volume.cumsum()
        cumulative_volume = volume.cumsum()

    result = cumulative_pv / cumulative_volume.replace(0, np.nan)
    result.name = "VWAP"
    return result


@register_indicator(
    name="obv",
    category="VOLUME",
    inputs=["close", "volume"],
    outputs=["obv"],
    parameters={},
    display_name="On Balance Volume",
    output_type="series",
    libraries=["built_in", "talib", "pandas_ta"],
    description="On Balance Volume",
    example="obv(close, volume)"
)
def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On Balance Volume."""
    if close.empty:
        return pd.Series(dtype=float, index=close.index, name="OBV")

    direction = np.sign(close.diff().fillna(0.0))
    result = (direction * volume).cumsum()
    result.name = "OBV"
    return result


__all__ = ["vwap", "obv"]

```

---

## File: `./patterns/__init__.py`

```python
"""
indicators/patterns
-------------------
Pattern recognition layer for candlesticks, chart patterns, and Dow Theory.
"""
from .candlestick import scan_all_candlestick
from .chart_patterns import scan_all_chart
from .dow_patterns import find_swings, trend_structure

__all__ = [
    "scan_all_candlestick",
    "scan_all_chart",
    "find_swings",
    "trend_structure",
]

```

---

## File: `./patterns/candlestick.py`

```python
"""
indicators/patterns/candlestick.py
----------------------------------
Candlestick pattern recognition.
Delegates to TA-Lib with a pure pandas fallback for core patterns.
"""
from typing import Callable
import pandas as pd
import numpy as np
from indicators.bridge import LibraryStatus
from indicators.registry import register_indicator


def _normalize_talib_output(series: pd.Series) -> pd.Series:
    """Normalize TA-Lib output from 100/-100 or 200/-200 to 1/-1/0."""
    return np.sign(series)


def _auto_select(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    talib_func_name: str,
    fallback_func: Callable[[pd.Series, pd.Series, pd.Series, pd.Series], pd.Series]
) -> pd.Series:
    if LibraryStatus.check("talib"):
        import talib
        func = getattr(talib, talib_func_name, None)
        if func is not None:
            op = open_.values.astype(float)
            hi = high.values.astype(float)
            lo = low.values.astype(float)
            cl = close.values.astype(float)
            res = pd.Series(func(op, hi, lo, cl), index=close.index)
            return _normalize_talib_output(res).astype(int)
    
    # fallback
    res = fallback_func(open_, high, low, close)
    return res.fillna(0).astype(int)


def get_candle_features(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series):
    o, h, l, c = open_, high, low, close
    body = (c - o).abs()
    real_body_top = pd.concat([o, c], axis=1).max(axis=1)
    real_body_bottom = pd.concat([o, c], axis=1).min(axis=1)
    upper_shadow = h - real_body_top
    lower_shadow = real_body_bottom - l
    rng = h - l
    is_bullish = c > o
    is_bearish = c < o
    return body, upper_shadow, lower_shadow, rng, is_bullish, is_bearish, real_body_top, real_body_bottom


@register_indicator(name="doji", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["doji"])
def doji(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        return (body <= rng * 0.1).astype(int)
    res = _auto_select(open_, high, low, close, "CDLDOJI", fallback)
    return (res == 1).astype(int)


@register_indicator(name="hammer", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["hammer"])
def hammer(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        return ((ls >= 2 * body) & (us <= rng * 0.1) & (rng > 0)).astype(int)
    res = _auto_select(open_, high, low, close, "CDLHAMMER", fallback)
    return (res == 1).astype(int)


@register_indicator(name="hanging_man", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["hanging_man"])
def hanging_man(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        return ((ls >= 2 * body) & (us <= rng * 0.1) & (body > 0)).astype(int) * -1
    return _auto_select(open_, high, low, close, "CDLHANGINGMAN", fallback)


@register_indicator(name="inverted_hammer", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["inverted_hammer"])
def inverted_hammer(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        return ((us >= 2 * body) & (ls <= rng * 0.1) & (body > 0)).astype(int)
    return _auto_select(open_, high, low, close, "CDLINVERTEDHAMMER", fallback)


@register_indicator(name="shooting_star", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["shooting_star"])
def shooting_star(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        return ((us >= 2 * body) & (ls <= rng * 0.1) & (body > 0)).astype(int) * -1
    return _auto_select(open_, high, low, close, "CDLSHOOTINGSTAR", fallback)


def _engulfing_fallback(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
    prev_bull = bull.shift(1)
    prev_bear = bear.shift(1)
    prev_body = body.shift(1)
    is_bull_eng = prev_bear & bull & (body > prev_body) & (rt > rt.shift(1)) & (rb < rb.shift(1))
    is_bear_eng = prev_bull & bear & (body > prev_body) & (rb < rb.shift(1)) & (rt > rt.shift(1))
    return is_bull_eng.astype(int) - is_bear_eng.astype(int)


@register_indicator(name="bullish_engulfing", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["bullish_engulfing"])
def bullish_engulfing(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    res = _auto_select(open_, high, low, close, "CDLENGULFING", _engulfing_fallback)
    return (res == 1).astype(int)


@register_indicator(name="bearish_engulfing", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["bearish_engulfing"])
def bearish_engulfing(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    res = _auto_select(open_, high, low, close, "CDLENGULFING", _engulfing_fallback)
    return (res == -1).astype(int)


@register_indicator(name="morning_star", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["morning_star"])
def morning_star(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        return (bear.shift(2) & (body.shift(1) <= rng.shift(1) * 0.3) & bull & (close > (open_.shift(2) + close.shift(2)) / 2)).astype(int)
    return _auto_select(open_, high, low, close, "CDLMORNINGSTAR", fallback)


@register_indicator(name="evening_star", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["evening_star"])
def evening_star(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        return (bull.shift(2) & (body.shift(1) <= rng.shift(1) * 0.3) & bear & (close < (open_.shift(2) + close.shift(2)) / 2)).astype(int) * -1
    return _auto_select(open_, high, low, close, "CDLEVENINGSTAR", fallback)


@register_indicator(name="three_white_soldiers", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["three_white_soldiers"])
def three_white_soldiers(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        return (bull & bull.shift(1) & bull.shift(2) & (close > close.shift(1)) & (close.shift(1) > close.shift(2))).astype(int)
    return _auto_select(open_, high, low, close, "CDL3WHITESOLDIERS", fallback)


@register_indicator(name="three_black_crows", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["three_black_crows"])
def three_black_crows(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        return (bear & bear.shift(1) & bear.shift(2) & (close < close.shift(1)) & (close.shift(1) < close.shift(2))).astype(int) * -1
    return _auto_select(open_, high, low, close, "CDL3BLACKCROWS", fallback)


@register_indicator(name="marubozu", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["marubozu"])
def marubozu(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        is_bull_maru = bull & (us <= rng * 0.05) & (ls <= rng * 0.05) & (body > 0)
        is_bear_maru = bear & (us <= rng * 0.05) & (ls <= rng * 0.05) & (body > 0)
        return is_bull_maru.astype(int) - is_bear_maru.astype(int)
    return _auto_select(open_, high, low, close, "CDLMARUBOZU", fallback)


@register_indicator(name="dragonfly_doji", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["dragonfly_doji"])
def dragonfly_doji(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        return ((body <= rng * 0.1) & (us <= rng * 0.1) & (ls > rng * 0.5)).astype(int)
    return _auto_select(open_, high, low, close, "CDLDRAGONFLYDOJI", fallback)


@register_indicator(name="gravestone_doji", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["gravestone_doji"])
def gravestone_doji(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        return ((body <= rng * 0.1) & (ls <= rng * 0.1) & (us > rng * 0.5)).astype(int)
    return _auto_select(open_, high, low, close, "CDLGRAVESTONEDOJI", fallback)


@register_indicator(name="harami", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["harami"])
def harami(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        is_bull_harami = bear.shift(1) & bull & (rt < rt.shift(1)) & (rb > rb.shift(1))
        is_bear_harami = bull.shift(1) & bear & (rt < rt.shift(1)) & (rb > rb.shift(1))
        return is_bull_harami.astype(int) - is_bear_harami.astype(int)
    return _auto_select(open_, high, low, close, "CDLHARAMI", fallback)


@register_indicator(name="harami_cross", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["harami_cross"])
def harami_cross(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        is_doji = body <= rng * 0.1
        is_bull_harami = bear.shift(1) & is_doji & (rt < rt.shift(1)) & (rb > rb.shift(1))
        is_bear_harami = bull.shift(1) & is_doji & (rt < rt.shift(1)) & (rb > rb.shift(1))
        return is_bull_harami.astype(int) - is_bear_harami.astype(int)
    return _auto_select(open_, high, low, close, "CDLHARAMICROSS", fallback)


@register_indicator(name="piercing_pattern", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["piercing_pattern"])
def piercing_pattern(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        midpoint_prev = (open_.shift(1) + close.shift(1)) / 2
        return (bear.shift(1) & bull & (open_ < rb.shift(1)) & (close > midpoint_prev) & (close < rt.shift(1))).astype(int)
    return _auto_select(open_, high, low, close, "CDLPIERCING", fallback)


@register_indicator(name="dark_cloud_cover", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["dark_cloud_cover"])
def dark_cloud_cover(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        midpoint_prev = (open_.shift(1) + close.shift(1)) / 2
        return (bull.shift(1) & bear & (open_ > rt.shift(1)) & (close < midpoint_prev) & (close > rb.shift(1))).astype(int) * -1
    return _auto_select(open_, high, low, close, "CDLDARKCLOUDCOVER", fallback)


@register_indicator(name="spinning_top", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["spinning_top"])
def spinning_top(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        is_spinning = (body <= rng * 0.3) & (us > body) & (ls > body)
        return is_spinning.astype(int) * (bull.astype(int) - bear.astype(int))
    return _auto_select(open_, high, low, close, "CDLSPINNINGTOP", fallback)


@register_indicator(name="tweezer_top", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["tweezer_top"])
def tweezer_top(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        is_tweezer = ((high - high.shift(1)).abs() < (high * 0.0001)) & (close < open_)
        return is_tweezer.astype(int) * -1
    return _auto_select(open_, high, low, close, "CDLTWEEZERTOP", fallback)


@register_indicator(name="tweezer_bottom", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["tweezer_bottom"])
def tweezer_bottom(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        is_tweezer = ((low - low.shift(1)).abs() < (low * 0.0001)) & (close > open_)
        return is_tweezer.astype(int)
    return _auto_select(open_, high, low, close, "CDLTWEEZERBOTTOM", fallback)


@register_indicator(name="abandoned_baby", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["abandoned_baby"])
def abandoned_baby(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        is_doji = body.shift(1) <= rng.shift(1) * 0.1
        bull_baby = bear.shift(2) & is_doji & bull & (low.shift(1) < low.shift(2)) & (low.shift(1) < low) & (high.shift(1) < low.shift(2)) & (high.shift(1) < low)
        bear_baby = bull.shift(2) & is_doji & bear & (high.shift(1) > high.shift(2)) & (high.shift(1) > high) & (low.shift(1) > high.shift(2)) & (low.shift(1) > high)
        return bull_baby.astype(int) - bear_baby.astype(int)
    return _auto_select(open_, high, low, close, "CDLABANDONEDBABY", fallback)


def _three_inside_fallback(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
    is_bull_harami = bear.shift(2) & bull.shift(1) & (rt.shift(1) < rt.shift(2)) & (rb.shift(1) > rb.shift(2))
    three_up = is_bull_harami & bull & (close > close.shift(1))
    is_bear_harami = bull.shift(2) & bear.shift(1) & (rt.shift(1) < rt.shift(2)) & (rb.shift(1) > rb.shift(2))
    three_down = is_bear_harami & bear & (close < close.shift(1))
    return three_up.astype(int) - three_down.astype(int)


@register_indicator(name="three_inside_up", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["three_inside_up"])
def three_inside_up(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    res = _auto_select(open_, high, low, close, "CDL3INSIDE", _three_inside_fallback)
    return (res == 1).astype(int)


@register_indicator(name="three_inside_down", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["three_inside_down"])
def three_inside_down(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    res = _auto_select(open_, high, low, close, "CDL3INSIDE", _three_inside_fallback)
    return (res == -1).astype(int)


def _three_outside_fallback(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
    is_bull_eng = bear.shift(2) & bull.shift(1) & (body.shift(1) > body.shift(2)) & (rt.shift(1) > rt.shift(2)) & (rb.shift(1) < rb.shift(2))
    three_up = is_bull_eng & bull & (close > close.shift(1))
    is_bear_eng = bull.shift(2) & bear.shift(1) & (body.shift(1) > body.shift(2)) & (rb.shift(1) < rb.shift(2)) & (rt.shift(1) > rt.shift(2))
    three_down = is_bear_eng & bear & (close < close.shift(1))
    return three_up.astype(int) - three_down.astype(int)


@register_indicator(name="three_outside_up", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["three_outside_up"])
def three_outside_up(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    res = _auto_select(open_, high, low, close, "CDL3OUTSIDE", _three_outside_fallback)
    return (res == 1).astype(int)


@register_indicator(name="three_outside_down", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["three_outside_down"])
def three_outside_down(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    res = _auto_select(open_, high, low, close, "CDL3OUTSIDE", _three_outside_fallback)
    return (res == -1).astype(int)


@register_indicator(name="kicking", category="PATTERN", inputs=["open", "high", "low", "close"], outputs=["kicking"])
def kicking(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    def fallback(open_, high, low, close):
        body, us, ls, rng, bull, bear, rt, rb = get_candle_features(open_, high, low, close)
        is_bull_maru_prev = bear.shift(1) & (us.shift(1) <= rng.shift(1) * 0.05) & (ls.shift(1) <= rng.shift(1) * 0.05)
        is_bull_maru = bull & (us <= rng * 0.05) & (ls <= rng * 0.05)
        bull_kick = is_bull_maru_prev & is_bull_maru & (open_ > open_.shift(1))
        
        is_bear_maru_prev = bull.shift(1) & (us.shift(1) <= rng.shift(1) * 0.05) & (ls.shift(1) <= rng.shift(1) * 0.05)
        is_bear_maru = bear & (us <= rng * 0.05) & (ls <= rng * 0.05)
        bear_kick = is_bear_maru_prev & is_bear_maru & (open_ < open_.shift(1))
        
        return bull_kick.astype(int) - bear_kick.astype(int)
    return _auto_select(open_, high, low, close, "CDLKICKING", fallback)


@register_indicator(
    name="scan_all_candlestick",
    category="PATTERN",
    inputs=["open", "high", "low", "close"],
    outputs=["doji", "engulfing", "hammer", "morning_star", "shooting_star"]
)
def scan_all_candlestick(df: pd.DataFrame) -> pd.DataFrame:
    """Scan for common candlestick patterns."""

    res = pd.DataFrame(index=df.index)
    res['doji'] = doji(df['open'], df['high'], df['low'], df['close'])
    res['engulfing'] = bullish_engulfing(df['open'], df['high'], df['low'], df['close']) - bearish_engulfing(df['open'], df['high'], df['low'], df['close'])
    res['hammer'] = hammer(df['open'], df['high'], df['low'], df['close'])
    res['morning_star'] = morning_star(df['open'], df['high'], df['low'], df['close'])
    res['shooting_star'] = shooting_star(df['open'], df['high'], df['low'], df['close'])
    
    # Adding the rest
    for name in ["hanging_man", "inverted_hammer", "three_white_soldiers", "three_black_crows",
                 "marubozu", "dragonfly_doji", "gravestone_doji", "harami", "harami_cross",
                 "piercing_pattern", "dark_cloud_cover", "spinning_top", "tweezer_top", 
                 "tweezer_bottom", "abandoned_baby", "three_inside_up", "three_inside_down",
                 "three_outside_up", "three_outside_down", "kicking", "evening_star"]:
        func = globals().get(name)
        if func:
            res[name] = func(df['open'], df['high'], df['low'], df['close'])
    
    return res



__all__ = [
    "doji",
    "hammer",
    "hanging_man",
    "inverted_hammer",
    "shooting_star",
    "bullish_engulfing",
    "bearish_engulfing",
    "morning_star",
    "evening_star",
    "three_white_soldiers",
    "three_black_crows",
    "marubozu",
    "dragonfly_doji",
    "gravestone_doji",
    "harami",
    "harami_cross",
    "piercing_pattern",
    "dark_cloud_cover",
    "spinning_top",
    "tweezer_top",
    "tweezer_bottom",
    "abandoned_baby",
    "three_inside_up",
    "three_inside_down",
    "three_outside_up",
    "three_outside_down",
    "kicking",
    "scan_all_candlestick"
]

```

---

## File: `./patterns/chart_patterns.py`

```python
"""
indicators/patterns/chart_patterns.py
-------------------------------------
Chart pattern recognition (triangles, double tops/bottoms).
"""
import numpy as np
import pandas as pd
from typing import Tuple

from indicators.registry import register_indicator

def _find_swing_highs(high: pd.Series, window: int = 5) -> pd.Series:
    """Find swing highs."""
    roll_max = high.rolling(window=2 * window + 1, center=True).max()
    return pd.Series(np.where(high == roll_max, high, np.nan), index=high.index)

def _find_swing_lows(low: pd.Series, window: int = 5) -> pd.Series:
    """Find swing lows."""
    roll_min = low.rolling(window=2 * window + 1, center=True).min()
    return pd.Series(np.where(low == roll_min, low, np.nan), index=low.index)

def _fit_trendline(x: np.ndarray, y: np.ndarray) -> Tuple[float, float]:
    """Fit a linear trendline y = mx + c. Returns (m, c)."""
    if len(x) < 2:
        return 0.0, 0.0
    x_mean = np.mean(x)
    y_mean = np.mean(y)
    denominator = np.sum((x - x_mean) ** 2)
    if denominator == 0:
        return 0.0, 0.0
    m = np.sum((x - x_mean) * (y - y_mean)) / denominator
    c = y_mean - m * x_mean
    return m, c

def _lines_converge(slope1: float, slope2: float, tolerance: float = 0.01) -> bool:
    """Check if lines are converging."""
    return (slope1 < -tolerance and slope2 > tolerance) or \
           (abs(slope1) < tolerance and slope2 > tolerance) or \
           (slope1 < -tolerance and abs(slope2) < tolerance) or \
           (slope1 > 0 and slope2 > 0 and slope1 < slope2) or \
           (slope1 < 0 and slope2 < 0 and slope1 < slope2)

def _lines_diverge(slope1: float, slope2: float, tolerance: float = 0.01) -> bool:
    """Check if lines are diverging."""
    return (slope1 > tolerance and slope2 < -tolerance)

def _get_recent_swings(swings: pd.Series, n: int = 3) -> pd.DataFrame:
    df = pd.DataFrame(index=swings.index)
    for i in range(1, n + 1):
        df[f'val_{i}'] = np.nan
        df[f'idx_{i}'] = np.nan
        
    valid_swings = swings.dropna()
    if valid_swings.empty:
        return df
        
    pos_idx = np.arange(len(swings))
    valid_pos = pos_idx[swings.notna()]
    swing_df = pd.DataFrame({'val': valid_swings, 'idx': valid_pos}, index=valid_swings.index)
    
    for i in range(1, n + 1):
        shifted = swing_df.shift(i - 1)
        df[f'val_{i}'] = shifted['val']
        df[f'idx_{i}'] = shifted['idx']
        
    return df.ffill()

def triangle_ascending(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify ascending triangle patterns (flat top, rising bottom).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = triangle_ascending(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rh = _get_recent_swings(highs, 2)
    rl = _get_recent_swings(lows, 2)
    
    h1, h2 = rh['val_1'], rh['val_2']
    h1_idx, h2_idx = rh['idx_1'], rh['idx_2']
    
    l1, l2 = rl['val_1'], rl['val_2']
    l1_idx, l2_idx = rl['idx_1'], rl['idx_2']
    
    d_h = (h1_idx - h2_idx).clip(lower=1)
    slope_top = (h1 - h2) / d_h
    d_l = (l1_idx - l2_idx).clip(lower=1)
    slope_bottom = (l1 - l2) / d_l
    
    cond_flat_top = (np.abs(h1 - h2) / np.maximum(h1, 1e-5)) < 0.02
    cond_rising_bottom = l1 > (l2 * 1.005)
    
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    top_line = h1 + slope_top * (curr_idx - h1_idx)
    top_line_prev = top_line - slope_top
    
    cond_breakout = (close > top_line) & (close.shift(1) <= top_line_prev)
    cond_time = (curr_idx > np.maximum(h1_idx, l1_idx))
    
    is_pattern = cond_flat_top & cond_rising_bottom & cond_breakout & cond_time
    res = pd.Series(0, index=close.index)
    res[is_pattern] = 1
    return res

def triangle_descending(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify descending triangle patterns (falling top, flat bottom).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = triangle_descending(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rh = _get_recent_swings(highs, 2)
    rl = _get_recent_swings(lows, 2)
    
    h1, h2 = rh['val_1'], rh['val_2']
    h1_idx, h2_idx = rh['idx_1'], rh['idx_2']
    
    l1, l2 = rl['val_1'], rl['val_2']
    l1_idx, l2_idx = rl['idx_1'], rl['idx_2']
    
    d_h = (h1_idx - h2_idx).clip(lower=1)
    slope_top = (h1 - h2) / d_h
    d_l = (l1_idx - l2_idx).clip(lower=1)
    slope_bottom = (l1 - l2) / d_l
    
    cond_flat_bottom = (np.abs(l1 - l2) / np.maximum(l1, 1e-5)) < 0.02
    cond_falling_top = h1 < (h2 * 0.995)
    
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    bottom_line = l1 + slope_bottom * (curr_idx - l1_idx)
    bottom_line_prev = bottom_line - slope_bottom
    
    cond_breakout = (close < bottom_line) & (close.shift(1) >= bottom_line_prev)
    cond_time = (curr_idx > np.maximum(h1_idx, l1_idx))
    
    is_pattern = cond_flat_bottom & cond_falling_top & cond_breakout & cond_time
    res = pd.Series(0, index=close.index)
    res[is_pattern] = -1
    return res

def triangle_symmetrical(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify symmetrical triangle patterns (converging trendlines).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = triangle_symmetrical(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rh = _get_recent_swings(highs, 2)
    rl = _get_recent_swings(lows, 2)
    
    h1, h2 = rh['val_1'], rh['val_2']
    h1_idx, h2_idx = rh['idx_1'], rh['idx_2']
    
    l1, l2 = rl['val_1'], rl['val_2']
    l1_idx, l2_idx = rl['idx_1'], rl['idx_2']
    
    d_h = (h1_idx - h2_idx).clip(lower=1)
    slope_top = (h1 - h2) / d_h
    d_l = (l1_idx - l2_idx).clip(lower=1)
    slope_bottom = (l1 - l2) / d_l
    
    cond_converge = (slope_top < 0) & (slope_bottom > 0)
    
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    top_line = h1 + slope_top * (curr_idx - h1_idx)
    top_line_prev = top_line - slope_top
    
    bottom_line = l1 + slope_bottom * (curr_idx - l1_idx)
    bottom_line_prev = bottom_line - slope_bottom
    
    cond_breakout_up = (close > top_line) & (close.shift(1) <= top_line_prev)
    cond_breakout_down = (close < bottom_line) & (close.shift(1) >= bottom_line_prev)
    cond_time = (curr_idx > np.maximum(h1_idx, l1_idx))
    
    res = pd.Series(0, index=close.index)
    res[cond_converge & cond_time & cond_breakout_up] = 1
    res[cond_converge & cond_time & cond_breakout_down] = -1
    return res

def flag_bullish(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify bullish flag patterns (downward parallel channel).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = flag_bullish(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rh = _get_recent_swings(highs, 2)
    rl = _get_recent_swings(lows, 2)
    
    h1, h2 = rh['val_1'], rh['val_2']
    h1_idx, h2_idx = rh['idx_1'], rh['idx_2']
    
    l1, l2 = rl['val_1'], rl['val_2']
    l1_idx, l2_idx = rl['idx_1'], rl['idx_2']
    
    d_h = (h1_idx - h2_idx).clip(lower=1)
    slope_top = (h1 - h2) / d_h
    d_l = (l1_idx - l2_idx).clip(lower=1)
    slope_bottom = (l1 - l2) / d_l
    
    cond_downward = (slope_top < 0) & (slope_bottom < 0)
    cond_parallel = (np.abs(slope_top - slope_bottom) / np.maximum(np.abs(slope_top), 1e-5)) < 0.5
    
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    top_line = h1 + slope_top * (curr_idx - h1_idx)
    top_line_prev = top_line - slope_top
    
    cond_breakout = (close > top_line) & (close.shift(1) <= top_line_prev)
    cond_time = (curr_idx > np.maximum(h1_idx, l1_idx))
    
    is_pattern = cond_downward & cond_parallel & cond_breakout & cond_time
    res = pd.Series(0, index=close.index)
    res[is_pattern] = 1
    return res

def flag_bearish(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify bearish flag patterns (upward parallel channel).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = flag_bearish(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rh = _get_recent_swings(highs, 2)
    rl = _get_recent_swings(lows, 2)
    
    h1, h2 = rh['val_1'], rh['val_2']
    h1_idx, h2_idx = rh['idx_1'], rh['idx_2']
    
    l1, l2 = rl['val_1'], rl['val_2']
    l1_idx, l2_idx = rl['idx_1'], rl['idx_2']
    
    d_h = (h1_idx - h2_idx).clip(lower=1)
    slope_top = (h1 - h2) / d_h
    d_l = (l1_idx - l2_idx).clip(lower=1)
    slope_bottom = (l1 - l2) / d_l
    
    cond_upward = (slope_top > 0) & (slope_bottom > 0)
    cond_parallel = (np.abs(slope_top - slope_bottom) / np.maximum(np.abs(slope_top), 1e-5)) < 0.5
    
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    bottom_line = l1 + slope_bottom * (curr_idx - l1_idx)
    bottom_line_prev = bottom_line - slope_bottom
    
    cond_breakout = (close < bottom_line) & (close.shift(1) >= bottom_line_prev)
    cond_time = (curr_idx > np.maximum(h1_idx, l1_idx))
    
    is_pattern = cond_upward & cond_parallel & cond_breakout & cond_time
    res = pd.Series(0, index=close.index)
    res[is_pattern] = -1
    return res

def pennant(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 3) -> pd.Series:
    """
    Identify pennant patterns (small symmetrical triangle).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 3.

    Returns:
        pd.Series: Series containing pattern signals.

    Example:
        >>> pattern_series = pennant(high, low, close, window=3)
    """
    return triangle_symmetrical(high, low, close, window)

def wedge_rising(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify rising wedge patterns (converging upward trendlines).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = wedge_rising(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rh = _get_recent_swings(highs, 2)
    rl = _get_recent_swings(lows, 2)
    
    h1, h2 = rh['val_1'], rh['val_2']
    h1_idx, h2_idx = rh['idx_1'], rh['idx_2']
    
    l1, l2 = rl['val_1'], rl['val_2']
    l1_idx, l2_idx = rl['idx_1'], rl['idx_2']
    
    d_h = (h1_idx - h2_idx).clip(lower=1)
    slope_top = (h1 - h2) / d_h
    d_l = (l1_idx - l2_idx).clip(lower=1)
    slope_bottom = (l1 - l2) / d_l
    
    cond_rising = (slope_top > 0) & (slope_bottom > 0)
    cond_converge = slope_bottom > slope_top
    
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    bottom_line = l1 + slope_bottom * (curr_idx - l1_idx)
    bottom_line_prev = bottom_line - slope_bottom
    
    cond_breakout = (close < bottom_line) & (close.shift(1) >= bottom_line_prev)
    cond_time = (curr_idx > np.maximum(h1_idx, l1_idx))
    
    is_pattern = cond_rising & cond_converge & cond_breakout & cond_time
    res = pd.Series(0, index=close.index)
    res[is_pattern] = -1
    return res

def wedge_falling(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify falling wedge patterns (converging downward trendlines).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = wedge_falling(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rh = _get_recent_swings(highs, 2)
    rl = _get_recent_swings(lows, 2)
    
    h1, h2 = rh['val_1'], rh['val_2']
    h1_idx, h2_idx = rh['idx_1'], rh['idx_2']
    
    l1, l2 = rl['val_1'], rl['val_2']
    l1_idx, l2_idx = rl['idx_1'], rl['idx_2']
    
    d_h = (h1_idx - h2_idx).clip(lower=1)
    slope_top = (h1 - h2) / d_h
    d_l = (l1_idx - l2_idx).clip(lower=1)
    slope_bottom = (l1 - l2) / d_l
    
    cond_falling = (slope_top < 0) & (slope_bottom < 0)
    cond_converge = slope_top < slope_bottom
    
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    top_line = h1 + slope_top * (curr_idx - h1_idx)
    top_line_prev = top_line - slope_top
    
    cond_breakout = (close > top_line) & (close.shift(1) <= top_line_prev)
    cond_time = (curr_idx > np.maximum(h1_idx, l1_idx))
    
    is_pattern = cond_falling & cond_converge & cond_breakout & cond_time
    res = pd.Series(0, index=close.index)
    res[is_pattern] = 1
    return res

def double_top(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify double top patterns (two similar highs with a dip between).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = double_top(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rh = _get_recent_swings(highs, 2)
    rl = _get_recent_swings(lows, 1)
    
    h1, h2 = rh['val_1'], rh['val_2']
    h1_idx, h2_idx = rh['idx_1'], rh['idx_2']
    
    l1 = rl['val_1']
    l1_idx = rl['idx_1']
    
    cond_height = (np.abs(h1 - h2) / np.maximum(h1, 1e-5)) < 0.03
    cond_order = (h1_idx > l1_idx) & (l1_idx > h2_idx)
    
    cond_breakout = (close < l1) & (close.shift(1) >= l1)
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    cond_time = curr_idx > h1_idx
    
    is_pattern = cond_height & cond_order & cond_breakout & cond_time
    res = pd.Series(0, index=close.index)
    res[is_pattern] = -1
    return res

def double_bottom(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify double bottom patterns (two similar lows with a bounce between).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = double_bottom(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rl = _get_recent_swings(lows, 2)
    rh = _get_recent_swings(highs, 1)
    
    l1, l2 = rl['val_1'], rl['val_2']
    l1_idx, l2_idx = rl['idx_1'], rl['idx_2']
    
    h1 = rh['val_1']
    h1_idx = rh['idx_1']
    
    cond_height = (np.abs(l1 - l2) / np.maximum(l1, 1e-5)) < 0.03
    cond_order = (l1_idx > h1_idx) & (h1_idx > l2_idx)
    
    cond_breakout = (close > h1) & (close.shift(1) <= h1)
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    cond_time = curr_idx > l1_idx
    
    is_pattern = cond_height & cond_order & cond_breakout & cond_time
    res = pd.Series(0, index=close.index)
    res[is_pattern] = 1
    return res

def head_and_shoulders(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify head and shoulders patterns (three peaks, middle is highest).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = head_and_shoulders(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rh = _get_recent_swings(highs, 3)
    rl = _get_recent_swings(lows, 2)
    
    h1, h2, h3 = rh['val_1'], rh['val_2'], rh['val_3']
    h1_idx, h2_idx, h3_idx = rh['idx_1'], rh['idx_2'], rh['idx_3']
    
    l1, l2 = rl['val_1'], rl['val_2']
    l1_idx, l2_idx = rl['idx_1'], rl['idx_2']
    
    cond_head = (h2 > h1) & (h2 > h3)
    cond_shoulders = (np.abs(h1 - h3) / np.maximum(np.maximum(h1, h3), 1e-5)) < 0.05
    cond_order = (h1_idx > l1_idx) & (l1_idx > h2_idx) & (h2_idx > l2_idx) & (l2_idx > h3_idx)
    
    d_l = (l1_idx - l2_idx).clip(lower=1)
    slope = (l1 - l2) / d_l
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    neckline = l1 + slope * (curr_idx - l1_idx)
    neckline_prev = neckline - slope
    
    cond_breakout = (close < neckline) & (close.shift(1) >= neckline_prev)
    cond_time = curr_idx > h1_idx
    
    is_pattern = cond_head & cond_shoulders & cond_order & cond_breakout & cond_time
    res = pd.Series(0, index=close.index)
    res[is_pattern] = -1
    return res

def inverse_head_and_shoulders(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify inverse head and shoulders patterns (three troughs, middle is lowest).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = inverse_head_and_shoulders(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rl = _get_recent_swings(lows, 3)
    rh = _get_recent_swings(highs, 2)
    
    l1, l2, l3 = rl['val_1'], rl['val_2'], rl['val_3']
    l1_idx, l2_idx, l3_idx = rl['idx_1'], rl['idx_2'], rl['idx_3']
    
    h1, h2 = rh['val_1'], rh['val_2']
    h1_idx, h2_idx = rh['idx_1'], rh['idx_2']
    
    cond_head = (l2 < l1) & (l2 < l3)
    cond_shoulders = (np.abs(l1 - l3) / np.maximum(np.maximum(l1, l3), 1e-5)) < 0.05
    cond_order = (l1_idx > h1_idx) & (h1_idx > l2_idx) & (l2_idx > h2_idx) & (h2_idx > l3_idx)
    
    d_h = (h1_idx - h2_idx).clip(lower=1)
    slope = (h1 - h2) / d_h
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    neckline = h1 + slope * (curr_idx - h1_idx)
    neckline_prev = neckline - slope
    
    cond_breakout = (close > neckline) & (close.shift(1) <= neckline_prev)
    cond_time = curr_idx > l1_idx
    
    is_pattern = cond_head & cond_shoulders & cond_order & cond_breakout & cond_time
    res = pd.Series(0, index=close.index)
    res[is_pattern] = 1
    return res

def cup_and_handle(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> pd.Series:
    """
    Identify cup and handle patterns (U-shape cup followed by slight downward drift).

    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        close (pd.Series): Close prices.
        window (int, optional): Swing window size. Defaults to 5.

    Returns:
        pd.Series: Series containing pattern signals (1 for bullish, -1 for bearish, 0 for none).

    Example:
        >>> pattern_series = cup_and_handle(high, low, close, window=5)
    """
    highs = _find_swing_highs(high, window)
    lows = _find_swing_lows(low, window)
    
    rh = _get_recent_swings(highs, 3)
    rl = _get_recent_swings(lows, 2)
    
    h1, h2, h3 = rh['val_1'], rh['val_2'], rh['val_3']
    h1_idx, h2_idx, h3_idx = rh['idx_1'], rh['idx_2'], rh['idx_3']
    
    l1, l2 = rl['val_1'], rl['val_2']
    l1_idx, l2_idx = rl['idx_1'], rl['idx_2']
    
    cond_lips = (np.abs(h3 - h2) / np.maximum(np.maximum(h3, h2), 1e-5)) < 0.05
    cond_handle_high = h1 < h2
    cond_cup_depth = (h2 - l2) / np.maximum(h2, 1e-5) > 0.05
    cond_handle_low = l1 > (l2 + (h2 - l2) * 0.3)
    
    cond_order = (h1_idx > l1_idx) & (l1_idx > h2_idx) & (h2_idx > l2_idx) & (l2_idx > h3_idx)
    
    curr_idx = pd.Series(np.arange(len(close)), index=close.index)
    cond_breakout = (close > h2) & (close.shift(1) <= h2)
    cond_time = curr_idx > h1_idx
    
    is_pattern = cond_lips & cond_handle_high & cond_cup_depth & cond_handle_low & cond_order & cond_breakout & cond_time
    res = pd.Series(0, index=close.index)
    res[is_pattern] = 1
    return res

@register_indicator(
    name="scan_all_chart",
    category="PATTERN",
    inputs=["high", "low", "close"],
    outputs=[
        "triangle_ascending", "triangle_descending", "triangle_symmetrical",
        "flag_bullish", "flag_bearish", "pennant", "wedge_rising", "wedge_falling",
        "double_top", "double_bottom", "head_and_shoulders", "inverse_head_and_shoulders",
        "cup_and_handle"
    ]
)
def scan_all_chart(df: pd.DataFrame) -> pd.DataFrame:
    """Scan for common chart patterns."""
    res = pd.DataFrame(index=df.index)
    
    if len(df) == 0:
        for col in ["triangle_ascending", "triangle_descending", "triangle_symmetrical",
                    "flag_bullish", "flag_bearish", "pennant", "wedge_rising", "wedge_falling",
                    "double_top", "double_bottom", "head_and_shoulders", "inverse_head_and_shoulders",
                    "cup_and_handle"]:
            res[col] = 0
        return res

    high = df['high']
    low = df['low']
    close = df['close']
    
    res['triangle_ascending'] = triangle_ascending(high, low, close)
    res['triangle_descending'] = triangle_descending(high, low, close)
    res['triangle_symmetrical'] = triangle_symmetrical(high, low, close)
    res['flag_bullish'] = flag_bullish(high, low, close)
    res['flag_bearish'] = flag_bearish(high, low, close)
    res['pennant'] = pennant(high, low, close)
    res['wedge_rising'] = wedge_rising(high, low, close)
    res['wedge_falling'] = wedge_falling(high, low, close)
    res['double_top'] = double_top(high, low, close)
    res['double_bottom'] = double_bottom(high, low, close)
    res['head_and_shoulders'] = head_and_shoulders(high, low, close)
    res['inverse_head_and_shoulders'] = inverse_head_and_shoulders(high, low, close)
    res['cup_and_handle'] = cup_and_handle(high, low, close)
    
    return res

__all__ = [
    "triangle_ascending", "triangle_descending", "triangle_symmetrical",
    "flag_bullish", "flag_bearish", "pennant", "wedge_rising", "wedge_falling",
    "double_top", "double_bottom", "head_and_shoulders", "inverse_head_and_shoulders",
    "cup_and_handle", "scan_all_chart"
]

```

---

## File: `./patterns/dow_patterns.py`

```python
"""
indicators/patterns/dow_patterns.py
-----------------------------------
Dow Theory patterns (higher highs, lower lows).
"""
import numpy as np
import pandas as pd
from indicators.registry import register_indicator

def swing_highs(series: pd.Series, order: int = 5) -> pd.Series:
    """Find swing highs in a series."""
    roll_max = series.rolling(window=2 * order + 1, center=True).max()
    return pd.Series(np.where(series == roll_max, series, np.nan), index=series.index)

def swing_lows(series: pd.Series, order: int = 5) -> pd.Series:
    """Find swing lows in a series."""
    roll_min = series.rolling(window=2 * order + 1, center=True).min()
    return pd.Series(np.where(series == roll_min, series, np.nan), index=series.index)

def higher_highs(series: pd.Series, order: int = 5) -> pd.Series:
    """Find higher highs."""
    highs = swing_highs(series, order)
    valid_highs = highs.dropna()
    is_hh = valid_highs > valid_highs.shift(1)
    res = pd.Series(False, index=series.index)
    if not is_hh.empty:
        res.loc[is_hh[is_hh].index] = True
    return res

def lower_highs(series: pd.Series, order: int = 5) -> pd.Series:
    """Find lower highs."""
    highs = swing_highs(series, order)
    valid_highs = highs.dropna()
    is_lh = valid_highs < valid_highs.shift(1)
    res = pd.Series(False, index=series.index)
    if not is_lh.empty:
        res.loc[is_lh[is_lh].index] = True
    return res

def higher_lows(series: pd.Series, order: int = 5) -> pd.Series:
    """Find higher lows."""
    lows = swing_lows(series, order)
    valid_lows = lows.dropna()
    is_hl = valid_lows > valid_lows.shift(1)
    res = pd.Series(False, index=series.index)
    if not is_hl.empty:
        res.loc[is_hl[is_hl].index] = True
    return res

def lower_lows(series: pd.Series, order: int = 5) -> pd.Series:
    """Find lower lows."""
    lows = swing_lows(series, order)
    valid_lows = lows.dropna()
    is_ll = valid_lows < valid_lows.shift(1)
    res = pd.Series(False, index=series.index)
    if not is_ll.empty:
        res.loc[is_ll[is_ll].index] = True
    return res


def find_swings(high: pd.Series, low: pd.Series, window: int = 5) -> pd.DataFrame:
    """Find local swing highs and lows."""
    # A point is a swing high if it's the highest in the window before and after it
    roll_high = high.rolling(window=2*window+1, center=True).max()
    roll_low = low.rolling(window=2*window+1, center=True).min()
    
    is_swing_high = (high == roll_high)
    is_swing_low = (low == roll_low)
    
    return pd.DataFrame({
        'swing_high': is_swing_high,
        'swing_low': is_swing_low,
        'swing_high_price': high.where(is_swing_high),
        'swing_low_price': low.where(is_swing_low)
    }, index=high.index)


@register_indicator(
    name="trend_structure",
    category="PATTERN",
    inputs=["high", "low"],
    outputs=["HH", "LH", "HL", "LL", "uptrend", "downtrend"],
    parameters={"window": 5}
)
def trend_structure(high: pd.Series, low: pd.Series, window: int = 5) -> pd.DataFrame:
    """
    Determine Higher Highs (HH), Higher Lows (HL), Lower Highs (LH), Lower Lows (LL).
    
    This function identifies the trend structure based on swing highs and lows,
    following Dow Theory principles. An uptrend is characterized by higher highs
    and higher lows, while a downtrend is characterized by lower highs and lower lows.
    
    Args:
        high (pd.Series): High prices.
        low (pd.Series): Low prices.
        window (int): The window size for identifying swings.
        
    Returns:
        pd.DataFrame: A DataFrame containing boolean columns for HH, LH, HL, LL,
            as well as uptrend and downtrend state indicators.
    """
    swings = find_swings(high, low, window)
    
    # Forward fill the last known swing prices
    last_high = swings['swing_high_price'].ffill()
    last_low = swings['swing_low_price'].ffill()
    
    # Previous known swings
    prev_high = last_high.shift(1).ffill()
    prev_low = last_low.shift(1).ffill()
    
    df = pd.DataFrame(index=high.index)
    df['HH'] = swings['swing_high'] & (swings['swing_high_price'] > prev_high)
    df['LH'] = swings['swing_high'] & (swings['swing_high_price'] < prev_high)
    df['HL'] = swings['swing_low'] & (swings['swing_low_price'] > prev_low)
    df['LL'] = swings['swing_low'] & (swings['swing_low_price'] < prev_low)
    
    # State tracking: uptrend requires latest completed swing high to be HH and low to be HL
    # For this, we forward fill the boolean flags of the latest swing
    last_is_hh = df['HH'].where(swings['swing_high']).ffill().fillna(False)
    last_is_hl = df['HL'].where(swings['swing_low']).ffill().fillna(False)
    last_is_lh = df['LH'].where(swings['swing_high']).ffill().fillna(False)
    last_is_ll = df['LL'].where(swings['swing_low']).ffill().fillna(False)
    
    df['uptrend'] = last_is_hh & last_is_hl
    df['downtrend'] = last_is_lh & last_is_ll
    
    return df

def trend_change(df: pd.DataFrame, order: int = 5) -> pd.Series:
    """Determine trend changes (-1, 0, 1) based on Dow Theory."""
    ts = trend_structure(df['high'], df['low'], window=order)
    res = pd.Series(0, index=df.index)
    res.loc[ts['uptrend']] = 1
    res.loc[ts['downtrend']] = -1
    return res

def market_phase(df: pd.DataFrame, lookback: int = 200) -> pd.Series:
    """
    Identify Wyckoff market phases.
    
    Returns:
        1: Accumulation
        2: Markup
        3: Distribution
        4: Markdown
    """
    close = df['close']
    fast = close.rolling(50).mean()
    slow = close.rolling(lookback).mean()
    
    phase = pd.Series(0, index=df.index)
    # Markup
    phase[(close > fast) & (fast > slow)] = 2
    # Markdown
    phase[(close < fast) & (fast < slow)] = 4
    # Accumulation
    phase[(close > fast) & (fast <= slow)] = 1
    # Distribution
    phase[(close < fast) & (fast >= slow)] = 3
    
    return phase

__all__ = [
    "swing_highs", "swing_lows", "higher_highs", "lower_highs", "higher_lows", "lower_lows",
    "find_swings", "trend_structure", "trend_change", "market_phase"
]

```

---

## File: `./support_resistance/__init__.py`

```python
"""
indicators/support_resistance
-----------------------------
Support and Resistance layer.
"""
from typing import List, Union, Optional
import pandas as pd

from .models import SRLevel, SRZone, sort_by_strength, filter_near_price
from .pivot import calculate_pivots, demark_pivots, to_sr_levels, weekly_pivots, monthly_pivots
from .price_based import calculate_swing_levels
from .fibonacci import fibonacci_retracements
from indicators.registry import register_indicator

@register_indicator(
    name="scan_all_levels",
    category="SUPPORT_RESISTANCE",
    inputs=["df"],
    outputs=["levels"],
    parameters={"methods": ["pivot", "swing", "fibonacci"]}
)
def scan_all_levels(df: pd.DataFrame, methods: List[str] = None) -> List[SRLevel]:
    """Scan and compile support/resistance levels from multiple methods."""
    if methods is None:
        methods = ["pivot", "swing", "fibonacci"]
        
    all_levels = []
    
    if "pivot" in methods and not df.empty:
        # Use previous bar for standard pivots if possible
        if len(df) >= 2:
            prev = df.iloc[-2]
            pivs = calculate_pivots(prev['high'], prev['low'], prev['close'])
            all_levels.extend(list(pivs.values()))
        else:
            last = df.iloc[-1]
            pivs = calculate_pivots(last['high'], last['low'], last['close'])
            all_levels.extend(list(pivs.values()))
            
    if "swing" in methods and not df.empty:
        swings = calculate_swing_levels(df['high'], df['low'], df['close'])
        all_levels.extend(swings)
        
    if "fibonacci" in methods and not df.empty:
        highest = df['high'].max()
        lowest = df['low'].min()
        last_close = df['close'].iloc[-1]
        trend = "up" if last_close > (highest + lowest) / 2 else "down"
        fibs = fibonacci_retracements(highest, lowest, trend=trend)
        all_levels.extend(fibs)
        
    return sort_by_strength(all_levels)

def nearest_support(levels: List[Union[SRLevel, SRZone]], current_price: float) -> Optional[Union[SRLevel, SRZone]]:
    """Find the nearest support level below the current price."""
    supports = []
    for l in levels:
        if isinstance(l, SRLevel):
            if getattr(l, "level_type", "unknown") in ("support", "pivot", "unknown") and l.price < current_price:
                supports.append(l)
        elif isinstance(l, SRZone):
            if getattr(l, "level_type", "unknown") in ("support", "zone", "unknown") and l.upper_price < current_price:
                supports.append(l)
                
    if not supports:
        return None
        
    return max(supports, key=lambda x: x.price if isinstance(x, SRLevel) else x.upper_price)

def nearest_resistance(levels: List[Union[SRLevel, SRZone]], current_price: float) -> Optional[Union[SRLevel, SRZone]]:
    """Find the nearest resistance level above the current price."""
    resistances = []
    for l in levels:
        if isinstance(l, SRLevel):
            if getattr(l, "level_type", "unknown") in ("resistance", "pivot", "unknown") and l.price > current_price:
                resistances.append(l)
        elif isinstance(l, SRZone):
            if getattr(l, "level_type", "unknown") in ("resistance", "zone", "unknown") and l.lower_price > current_price:
                resistances.append(l)
                
    if not resistances:
        return None
        
    return min(resistances, key=lambda x: x.price if isinstance(x, SRLevel) else x.lower_price)

__all__ = [
    "SRLevel", 
    "SRZone",
    "sort_by_strength",
    "filter_near_price",
    "calculate_pivots", 
    "demark_pivots",
    "to_sr_levels",
    "weekly_pivots",
    "monthly_pivots",
    "calculate_swing_levels", 
    "fibonacci_retracements",
    "scan_all_levels",
    "nearest_support",
    "nearest_resistance"
]

```

---

## File: `./support_resistance/fibonacci.py`

```python
"""
indicators/support_resistance/fibonacci.py
------------------------------------------
Fibonacci retracements and extensions.
"""
import pandas as pd
from typing import List, Tuple
from indicators.support_resistance.models import SRLevel
from indicators.registry import register_indicator

FIBO_RETRACEMENT_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
FIBO_EXTENSION_LEVELS = [1.272, 1.414, 1.618, 2.0, 2.618, 3.618, 4.236]

def _price_at_level(high: float, low: float, level: float, trend: str = "up") -> float:
    """Calculate the price at a specific Fibonacci level."""
    diff = high - low
    if trend == "up":
        return high - (diff * level)
    else:
        return low + (diff * level)

def _find_major_swing(high: pd.Series, low: pd.Series, window: int = 50) -> Tuple[float, float, str]:
    """Find the major swing high and low over a lookback window."""
    h = high.tail(window).max()
    l = low.tail(window).min()
    
    h_idx = high.tail(window).idxmax()
    l_idx = low.tail(window).idxmin()
    
    # If the high occurred after the low, it's an upward swing
    trend = "up" if h_idx > l_idx else "down"
    
    return h, l, trend

@register_indicator(
    name="fibonacci_retracements",
    category="SUPPORT_RESISTANCE",
    inputs=["high", "low"],
    outputs=["levels"],
    parameters={"trend": "up"}
)
def fibonacci_retracements(high: float, low: float, trend: str = "up") -> list[SRLevel]:
    """Calculate Fibonacci retracement levels for a given price range."""
    res = []
    for level in FIBO_RETRACEMENT_LEVELS:
        price = _price_at_level(high, low, level, trend)
        level_type = "support" if trend == "up" else "resistance"
        res.append(SRLevel(price=price, level_type=level_type, source="fibonacci"))
        
    return res

@register_indicator(
    name="fibonacci_extensions",
    category="SUPPORT_RESISTANCE",
    inputs=["high", "low"],
    outputs=["levels"],
    parameters={"trend": "up"}
)
def fibonacci_extensions(high: float, low: float, trend: str = "up") -> list[SRLevel]:
    """Calculate Fibonacci extension levels for a given price range."""
    res = []
    for level in FIBO_EXTENSION_LEVELS:
        price = _price_at_level(high, low, level, trend)
        level_type = "support" if trend == "up" else "resistance"
        res.append(SRLevel(price=price, level_type=level_type, source="fibonacci_extension"))
        
    return res

@register_indicator(
    name="fibonacci_fans",
    category="SUPPORT_RESISTANCE",
    inputs=["high", "low"],
    outputs=["levels"],
    parameters={"window": 50}
)
def fibonacci_fans(df: pd.DataFrame, lookback: int = 50) -> list[SRLevel]:
    """Calculate Fibonacci Fan levels based on recent swing."""
    if len(df) < lookback:
        return []
        
    h, l, trend = _find_major_swing(df['high'], df['low'], lookback)
    
    # Just reusing retracements logic for simplicity of price levels at the fan edge.
    # A true fan is diagonal, but returning static SRLevels is the required contract.
    res = []
    for level in [0.382, 0.5, 0.618]:
        price = _price_at_level(h, l, level, trend)
        level_type = "support" if trend == "up" else "resistance"
        res.append(SRLevel(price=price, level_type=level_type, source="fibonacci_fan"))
        
    return res

@register_indicator(
    name="auto_fibonacci",
    category="SUPPORT_RESISTANCE",
    inputs=["high", "low"],
    outputs=["levels"],
    parameters={"window": 50}
)
def auto_fibonacci(high: pd.Series, low: pd.Series, window: int = 50) -> list[SRLevel]:
    """Automatically find major swing and calculate retracements and extensions."""
    if len(high) < window or len(low) < window:
        return []
        
    h, l, trend = _find_major_swing(high, low, window)
    
    retracements = fibonacci_retracements(h, l, trend)
    extensions = fibonacci_extensions(h, l, trend)
    
    return retracements + extensions

__all__ = [
    "FIBO_RETRACEMENT_LEVELS",
    "FIBO_EXTENSION_LEVELS",
    "_price_at_level",
    "_find_major_swing",
    "fibonacci_retracements",
    "fibonacci_extensions",
    "auto_fibonacci",
    "fibonacci_fans"
]

```

---

## File: `./support_resistance/models.py`

```python
"""
indicators/support_resistance/models.py
---------------------------------------
Data models for Support and Resistance levels and zones.
"""
from dataclasses import dataclass
import pandas as pd

@dataclass
class SRLevel:
    """A distinct price level acting as support or resistance."""
    price: float
    strength: float = 1.0  # 0.0 to 1.0
    level_type: str = "unknown"  # "support", "resistance", "pivot"
    source: str = "unknown" # e.g., "pivot", "swing", "fibonacci"
    touches: int = 0
    last_tested: pd.Timestamp | None = None

    def to_dict(self):
        return {
            "price": self.price,
            "strength": self.strength,
            "level_type": self.level_type,
            "source": self.source,
            "touches": self.touches,
            "last_tested": self.last_tested
        }

@dataclass
class SRZone:
    """A price zone acting as support or resistance (e.g. from clustered swings)."""
    lower_price: float
    upper_price: float
    strength: float = 1.0
    level_type: str = "zone"
    source: str = "unknown"

    def to_dict(self):
        return {
            "lower_price": self.lower_price,
            "upper_price": self.upper_price,
            "strength": self.strength,
            "level_type": self.level_type,
            "source": self.source
        }

def sort_by_strength(items):
    """Sort levels or zones by strength descending."""
    return sorted(items, key=lambda x: x.strength, reverse=True)

def filter_near_price(items, price: float, threshold: float = 0.01):
    """Filter levels or zones near a given price within a percentage threshold (0.01 = 1%)."""
    result = []
    for item in items:
        if isinstance(item, SRLevel):
            if abs(item.price - price) / price <= threshold:
                result.append(item)
        elif isinstance(item, SRZone):
            # Distance to nearest edge of the zone
            dist = 0.0
            if price < item.lower_price:
                dist = (item.lower_price - price) / price
            elif price > item.upper_price:
                dist = (price - item.upper_price) / price
            if dist <= threshold:
                result.append(item)
    return result

__all__ = ["SRLevel", "SRZone", "sort_by_strength", "filter_near_price"]

```

---

## File: `./support_resistance/pivot.py`

```python
"""
indicators/support_resistance/pivot.py
--------------------------------------
Pivot Point calculations.
"""
from typing import Dict, List
import pandas as pd
from indicators.support_resistance.models import SRLevel
from indicators.registry import register_indicator

@register_indicator(
    name="calculate_pivots",
    category="SUPPORT_RESISTANCE",
    inputs=["high", "low", "close"],
    outputs=["P", "R1", "S1", "R2", "S2", "R3", "S3", "R4", "S4"],
    parameters={"method": "classic"}
)

def calculate_pivots(high: float, low: float, close: float, method: str = "classic") -> Dict[str, SRLevel]:
    """
    Calculate pivot points based on previous period OHLC.
    Methods: classic, camarilla, woodie, demark.
    """
    pivots = {}
    
    if method == "classic":
        p = (high + low + close) / 3
        pivots["P"] = SRLevel(price=p, level_type="pivot", source="classic")
        pivots["R1"] = SRLevel(price=(2 * p) - low, level_type="resistance", source="classic")
        pivots["S1"] = SRLevel(price=(2 * p) - high, level_type="support", source="classic")
        pivots["R2"] = SRLevel(price=p + (high - low), level_type="resistance", source="classic")
        pivots["S2"] = SRLevel(price=p - (high - low), level_type="support", source="classic")
        pivots["R3"] = SRLevel(price=p + 2 * (high - low), level_type="resistance", source="classic")
        pivots["S3"] = SRLevel(price=p - 2 * (high - low), level_type="support", source="classic")
        
    elif method == "woodie":
        p = (high + low + 2 * close) / 4
        pivots["P"] = SRLevel(price=p, level_type="pivot", source="woodie")
        pivots["R1"] = SRLevel(price=(2 * p) - low, level_type="resistance", source="woodie")
        pivots["S1"] = SRLevel(price=(2 * p) - high, level_type="support", source="woodie")
        pivots["R2"] = SRLevel(price=p + high - low, level_type="resistance", source="woodie")
        pivots["S2"] = SRLevel(price=p - high + low, level_type="support", source="woodie")
        
    elif method == "camarilla":
        range_hl = high - low
        pivots["R4"] = SRLevel(price=close + (range_hl * 1.1) / 2, level_type="resistance", source="camarilla")
        pivots["R3"] = SRLevel(price=close + (range_hl * 1.1) / 4, level_type="resistance", source="camarilla")
        pivots["R2"] = SRLevel(price=close + (range_hl * 1.1) / 6, level_type="resistance", source="camarilla")
        pivots["R1"] = SRLevel(price=close + (range_hl * 1.1) / 12, level_type="resistance", source="camarilla")
        pivots["S1"] = SRLevel(price=close - (range_hl * 1.1) / 12, level_type="support", source="camarilla")
        pivots["S2"] = SRLevel(price=close - (range_hl * 1.1) / 6, level_type="support", source="camarilla")
        pivots["S3"] = SRLevel(price=close - (range_hl * 1.1) / 4, level_type="support", source="camarilla")
        pivots["S4"] = SRLevel(price=close - (range_hl * 1.1) / 2, level_type="support", source="camarilla")

    elif method == "fibonacci":
        p = (high + low + close) / 3
        range_hl = high - low
        pivots["P"] = SRLevel(price=p, level_type="pivot", source="fibonacci")
        pivots["R1"] = SRLevel(price=p + 0.382 * range_hl, level_type="resistance", source="fibonacci")
        pivots["R2"] = SRLevel(price=p + 0.618 * range_hl, level_type="resistance", source="fibonacci")
        pivots["R3"] = SRLevel(price=p + 1.000 * range_hl, level_type="resistance", source="fibonacci")
        pivots["S1"] = SRLevel(price=p - 0.382 * range_hl, level_type="support", source="fibonacci")
        pivots["S2"] = SRLevel(price=p - 0.618 * range_hl, level_type="support", source="fibonacci")
        pivots["S3"] = SRLevel(price=p - 1.000 * range_hl, level_type="support", source="fibonacci")

    return pivots

def demark_pivots(open_price: float, high: float, low: float, close: float) -> Dict[str, SRLevel]:
    """Calculate DeMark pivot points."""
    if close < open_price:
        x = high + (2 * low) + close
    elif close > open_price:
        x = (2 * high) + low + close
    else:
        x = high + low + (2 * close)
        
    p = x / 4
    r1 = x / 2 - low
    s1 = x / 2 - high
    
    return {
        "P": SRLevel(price=p, level_type="pivot", source="demark"),
        "R1": SRLevel(price=r1, level_type="resistance", source="demark"),
        "S1": SRLevel(price=s1, level_type="support", source="demark")
    }

def to_sr_levels(pivots_dict: Dict[str, SRLevel]) -> List[SRLevel]:
    """Convert pivot dictionary to a list of SRLevel objects."""
    return list(pivots_dict.values())

def weekly_pivots(df: pd.DataFrame, method: str = "classic") -> Dict[str, SRLevel]:
    """Calculate weekly pivot points using resampled OHLC."""
    if df.empty or len(df) < 2:
        return {}
    
    # Capital 'W' for weekly resample
    weekly = df.resample('W').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last'
    }).dropna()
    
    if len(weekly) < 2:
        return {}
    
    # Previous completed week
    prev = weekly.iloc[-2]
    if method == "demark":
        return demark_pivots(prev['open'], prev['high'], prev['low'], prev['close'])
    return calculate_pivots(prev['high'], prev['low'], prev['close'], method=method)

def monthly_pivots(df: pd.DataFrame, method: str = "classic") -> Dict[str, SRLevel]:
    """Calculate monthly pivot points using resampled OHLC."""
    if df.empty or len(df) < 2:
        return {}
        
    monthly = df.resample('ME').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last'
    }).dropna()
    
    if len(monthly) < 2:
        return {}
    
    # Previous completed month
    prev = monthly.iloc[-2]
    if method == "demark":
        return demark_pivots(prev['open'], prev['high'], prev['low'], prev['close'])
    return calculate_pivots(prev['high'], prev['low'], prev['close'], method=method)

__all__ = ["calculate_pivots", "demark_pivots", "to_sr_levels", "weekly_pivots", "monthly_pivots"]

```

---

## File: `./support_resistance/price_based.py`

```python
"""
indicators/support_resistance/price_based.py
--------------------------------------------
Price-based support/resistance using historical swings and clusters.
"""
import math
import numpy as np
import pandas as pd
from typing import List

from indicators.support_resistance.models import SRLevel, SRZone
from indicators.patterns.dow_patterns import find_swings
from indicators.registry import register_indicator

def _cluster_levels(levels: List[float], tolerance: float = 0.005) -> List[float]:
    """Cluster similar price levels."""
    if not levels:
        return []
    
    levels = sorted(levels)
    clusters = []
    current_cluster = [levels[0]]
    
    for level in levels[1:]:
        if level <= current_cluster[0] * (1 + tolerance):
            current_cluster.append(level)
        else:
            clusters.append(sum(current_cluster) / len(current_cluster))
            current_cluster = [level]
            
    if current_cluster:
        clusters.append(sum(current_cluster) / len(current_cluster))
        
    return clusters

def _score_level(price: float, closes: pd.Series, tolerance: float = 0.005) -> float:
    """Score a price level based on historical interactions (touches)."""
    if closes.empty:
        return 0.0
    upper = price * (1 + tolerance)
    lower = price * (1 - tolerance)
    touches = ((closes >= lower) & (closes <= upper)).sum()
    # Normalize score between 0 and 1, assuming e.g. 10 touches is "max" strength
    return min(touches / 10.0, 1.0)

@register_indicator(
    name="calculate_swing_levels",
    category="SUPPORT_RESISTANCE",
    inputs=["high", "low", "close"],
    outputs=["levels"],
    parameters={"window": 5}
)
def calculate_swing_levels(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 5) -> List[SRLevel]:
    """Find key support/resistance levels from recent swing highs/lows."""
    swings = find_swings(high, low, window)
    levels = []
    
    # Extract unique swing highs and lows
    swing_highs = swings['swing_high_price'].dropna().unique()
    swing_lows = swings['swing_low_price'].dropna().unique()
    
    for price in swing_highs:
        strength = _score_level(price, close)
        levels.append(SRLevel(price=price, level_type="resistance", source="swing", strength=strength))
        
    for price in swing_lows:
        strength = _score_level(price, close)
        levels.append(SRLevel(price=price, level_type="support", source="swing", strength=strength))
        
    return levels

@register_indicator(
    name="volume_profile_sr",
    category="SUPPORT_RESISTANCE",
    inputs=["close", "volume"],
    outputs=["levels"],
    parameters={"bins": 50, "prominence": 1.5}
)
def volume_profile_sr(close: pd.Series, volume: pd.Series, bins: int = 50, prominence: float = 1.5) -> List[SRLevel]:
    """Find Support/Resistance levels using High Volume Nodes (HVNs) in Volume Profile."""
    if close.empty or volume.empty:
        return []
    
    min_price = close.min()
    max_price = close.max()
    
    if min_price == max_price:
        return []
        
    bin_edges = np.linspace(min_price, max_price, bins + 1)
    
    # Use pandas cut to group volumes
    volume_by_price = volume.groupby(pd.cut(close, bins=bin_edges, include_lowest=True), observed=False).sum()
    
    # Find peaks (HVNs)
    mean_volume = volume_by_price.mean()
    threshold = mean_volume * prominence
    
    hvns = volume_by_price[volume_by_price > threshold]
    
    levels = []
    for interval, vol in hvns.items():
        if pd.notna(interval):
            price = interval.mid
            levels.append(SRLevel(price=price, level_type="unknown", source="volume_profile"))
            
    return levels

@register_indicator(
    name="round_number_sr",
    category="SUPPORT_RESISTANCE",
    inputs=["close"],
    outputs=["levels"],
    parameters={"levels_count": 5}
)
def round_number_sr(close: pd.Series, levels_count: int = 5) -> List[SRLevel]:
    """Identify psychological support and resistance at round numbers."""
    if close.empty:
        return []
    
    current_price = close.iloc[-1]
    
    if current_price == 0:
        return []
        
    oom = 10 ** math.floor(math.log10(current_price))
    
    if current_price / oom < 5:
        step = oom / 10
    else:
        step = oom / 2
        
    if step == 0:
        return []
        
    closest_round = round(current_price / step) * step
    
    levels = []
    for i in range(-levels_count, levels_count + 1):
        price = closest_round + (i * step)
        if price <= 0:
            continue
        level_type = "resistance" if price > current_price else ("support" if price < current_price else "current")
        levels.append(SRLevel(price=price, level_type=level_type, source="round_number"))
        
    return levels

@register_indicator(
    name="recent_high_low",
    category="SUPPORT_RESISTANCE",
    inputs=["high", "low", "close"],
    outputs=["levels"],
    parameters={"window": 20}
)
def recent_high_low(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 20) -> List[SRLevel]:
    """Calculate support and resistance from recent N-period highs and lows."""
    if len(high) < window or len(low) < window:
        return []
        
    recent_high = high.tail(window).max()
    recent_low = low.tail(window).min()
    current_price = close.iloc[-1]
    
    levels = []
    if pd.notna(recent_high):
        levels.append(SRLevel(price=recent_high, level_type="resistance", source="recent_high"))
    if pd.notna(recent_low):
        levels.append(SRLevel(price=recent_low, level_type="support", source="recent_low"))
        
    return levels

__all__ = [
    "calculate_swing_levels",
    "volume_profile_sr",
    "round_number_sr",
    "recent_high_low"
]

```

---

