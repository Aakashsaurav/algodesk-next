"""Backtest benchmark: measures execution speed, throughput, and memory with cProfile."""

from __future__ import annotations

import cProfile
import io
import math
import pstats
import shutil
import tempfile
import time
import tracemalloc
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from algodesk.api.kinds import Mode, PluginKind
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, InstrumentId, SeriesKey
from algodesk.api.protocols import DataSource
from algodesk.data.manifest import DataManifest
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime


@dataclass(frozen=True)
class BenchmarkReport:
    """Benchmark performance report."""

    symbols: int
    sessions: int
    timeframe: str
    total_bars: int
    elapsed_seconds: float
    bars_per_second: float
    peak_memory_mb: float
    profile_stats_text: str
    fixed_cost_seconds: float = 0.0
    per_bar_cost_seconds: float = 0.0


class FakeSource(DataSource):
    """Synthetic DataSource delivering generated benchmark bars."""

    def __init__(self, b_map: dict[InstrumentId, list[Bar]]) -> None:
        self.b_map = b_map
        self.manifest = PluginManifest(
            name="upstox",
            version="0.1.0",
            kind=PluginKind.BROKER,
            api_version=1,
            entry="plugins.brokers.upstox.broker:UpstoxBroker",
            capabilities={"history": True},
            retention={"1d": 36500, "1m": 3650},
            max_span={"1d": 3650, "1m": 365},
        )

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        bars = self.b_map.get(key.instrument, [])
        return [b for b in bars if start <= b.start_ts <= end]

    def instruments(self) -> Iterable[Any]:
        return []

    def stream(self, insts: Any) -> Any:
        raise NotImplementedError


def _generate_trading_days(start_date: date, count: int) -> list[date]:
    """Generate consecutive Monday-Friday trading calendar dates."""
    sessions: list[date] = []
    cur_d = start_date
    while len(sessions) < count:
        if cur_d.weekday() < 5:
            sessions.append(cur_d)
        cur_d += timedelta(days=1)
    return sessions


def _build_synthetic_bars(
    symbols: list[InstrumentId],
    sessions: list[date],
    timeframe: str,
) -> tuple[dict[InstrumentId, list[Bar]], int]:
    """Generate synthetic deterministic bars for benchmark execution."""
    bars_by_symbol: dict[InstrumentId, list[Bar]] = {}
    total_bars_count = 0
    bars_per_sess = 1 if timeframe == "1d" else 375

    for idx, sym in enumerate(symbols):
        sym_bars: list[Bar] = []
        base_price = 100.0 + (idx % 20) * 10.0
        for s_idx, d in enumerate(sessions):
            s_open = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)
            for m_idx in range(bars_per_sess):
                b_start = s_open if timeframe == "1d" else s_open + timedelta(minutes=m_idx)
                b_end = (
                    datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC)
                    if timeframe == "1d"
                    else b_start + timedelta(minutes=1)
                )
                step = s_idx if timeframe == "1d" else s_idx * bars_per_sess + m_idx
                factor = 1.0 + 0.05 * math.sin(step * 2 * math.pi / 50.0)
                c_price = round(base_price * factor, 2)
                o_price = round(c_price * 0.999, 2)
                h_price = round(max(o_price, c_price) * 1.002, 2)
                l_price = round(min(o_price, c_price) * 0.998, 2)
                sym_bars.append(
                    Bar(
                        instrument=sym,
                        timeframe=timeframe,
                        start_ts=b_start,
                        end_ts=b_end,
                        open=o_price,
                        high=h_price,
                        low=l_price,
                        close=c_price,
                        volume=1000,
                        complete=True,
                        synthetic=False,
                    )
                )
        bars_by_symbol[sym] = sym_bars
        total_bars_count += len(sym_bars)
    return bars_by_symbol, total_bars_count


def _write_bench_config(
    cfg_file: Path,
    symbols: list[InstrumentId],
    sessions: list[date],
    timeframe: str,
) -> None:
    """Write benchmark run configuration to disk."""
    sym_list_str = ", ".join(f'"{s}"' for s in symbols)
    cfg_content = f"""allocated_capital = 10000000.0

[run]
name = "{cfg_file.stem}"
mode = "backtest"

[backtest]
start = "{sessions[0].isoformat()}"
end = "{sessions[-1].isoformat()}"
fidelity = "bar"
timeframe = "{timeframe}"

[data_source]
plugin = "upstox"

[execution_venue]
plugin = "sim"
seed = 42

[fees]
schedule = "zerodha_nse"
fallback = "earliest"

[[calendars]]
plugin = "exchange"
exchange = "nse"

[secrets]
plugin = "keychain"

[universe]
symbols = [{sym_list_str}]

[[strategies]]
plugin = "sma_cross"
risk_profile = "conservative"
params = {{ timeframe = "{timeframe}", fast = 5, slow = 15, qty = 1, product = "CNC" }}
"""
    cfg_file.write_text(cfg_content, encoding="utf-8")


def _filter_and_format_profile(prof: cProfile.Profile, top_n: int = 10) -> str:
    """Filter profile stats to algodesk and plugins, sorted by tottime."""
    s_buf = io.StringIO()
    ps: Any = pstats.Stats(prof, stream=s_buf)
    filtered: dict[Any, Any] = {}
    for k, v in ps.stats.items():
        fname = k[0]
        is_proj = (
            "/src/algodesk/" in fname
            or "/plugins/" in fname
            or fname.startswith("src/algodesk")
            or fname.startswith("plugins")
        )
        is_external = (
            ".local/python" in fname
            or "site-packages" in fname
            or ".venv" in fname
            or "/tests/" in fname
        )
        if is_proj and not is_external:
            filtered[k] = v
    ps.stats = filtered
    ps.sort_stats("tottime")
    ps.print_stats(top_n)
    return s_buf.getvalue()


def _run_benchmark_once(
    project_dir: Path,
    symbols: list[InstrumentId],
    sessions: list[date],
    timeframe: str,
    with_profile: bool = True,
) -> tuple[int, float, float, cProfile.Profile | None]:
    """Execute one benchmark backtest pass and return (bars_processed, elapsed, peak_mb, prof)."""
    bars_by_sym, total_bars = _build_synthetic_bars(symbols, sessions, timeframe)
    data_dir = project_dir / "data"
    m_path = data_dir / "state" / f"data_manifest_{len(symbols)}x{len(sessions)}.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    store = BarStore(data_dir=data_dir, manifest=manifest)
    fake_src = FakeSource(bars_by_sym)
    ds = DataService(sources={"upstox": fake_src}, default_source="upstox", store=store)
    s_start = datetime(sessions[0].year, sessions[0].month, sessions[0].day, 0, 0, tzinfo=UTC)
    s_end = datetime(sessions[-1].year, sessions[-1].month, sessions[-1].day, 23, 59, tzinfo=UTC)
    for sym in symbols:
        ds.get(f"{sym}:{timeframe}", start=s_start, end=s_end, source="upstox", cache_only=False)

    cfg_file = project_dir / "config" / "runs" / f"bench_{len(symbols)}x{len(sessions)}.toml"
    _write_bench_config(cfg_file, symbols, sessions, timeframe)
    bench_name = f"bench_{len(symbols)}x{len(sessions)}"

    with open_runtime(project_dir, run=bench_name, ensure=True) as rt:
        runner = SessionRunner(
            runtime=rt,
            run_config=rt.effective.run,
            mode=Mode.BACKTEST,
            quiet=False,
        )
        runner._data_service = ds
        import asyncio

        tracemalloc.start()
        prof = cProfile.Profile() if with_profile else None
        t0 = time.perf_counter()
        if prof:
            prof.enable()
        try:
            asyncio.run(runner.run())
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        if prof:
            prof.disable()
        t1 = time.perf_counter()
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        elapsed = max(0.001, t1 - t0)
        actual_bars = getattr(runner, "_bars_closed", 0)
        is_stopped = bool(
            getattr(runner, "_interrupted", False)
            or getattr(runner, "_user_interrupted", False)
            or getattr(runner, "_stop_reason", "") in ("interrupt", "benchmark_timeout")
        )
        bars_processed = actual_bars if (is_stopped and actual_bars > 0) else total_bars
        peak_mb = peak_bytes / (1024 * 1024)
        manifest.close()
        return bars_processed, elapsed, peak_mb, prof


def run_benchmark_measurement(
    symbols_count: int = 100,
    sessions_count: int = 250,
    timeframe: str = "1d",
    profile: bool = True,
) -> BenchmarkReport:
    """Execute synthetic cache backtest measurement in a temp directory outside the repo."""
    root_dir = Path(__file__).resolve().parents[3]

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        project_dir = Path(tmp_dir_str) / "bench_proj"
        project_dir.mkdir(parents=True, exist_ok=True)

        shutil.copyfile(root_dir / "pyproject.toml", project_dir / "pyproject.toml")
        shutil.copytree(root_dir / "config", project_dir / "config")
        shutil.copytree(root_dir / "plugins", project_dir / "plugins")
        (project_dir / "data").mkdir(parents=True, exist_ok=True)
        (project_dir / "logs").mkdir(parents=True, exist_ok=True)
        (project_dir / "results").mkdir(parents=True, exist_ok=True)
        (project_dir / ".algodesk").mkdir(parents=True, exist_ok=True)
        conf_json = root_dir / ".algodesk" / "conformance.json"
        if conf_json.is_file():
            shutil.copyfile(conf_json, project_dir / ".algodesk" / "conformance.json")

        symbols = [InstrumentId(f"NSE:EQ:SYM{i:03d}") for i in range(symbols_count)]
        sessions = _generate_trading_days(date(2025, 1, 1), sessions_count)

        # 1. Target run
        bars_1, elapsed_1, peak_mb_1, prof_1 = _run_benchmark_once(
            project_dir, symbols, sessions, timeframe, with_profile=profile
        )

        # 2. Baseline run (smaller size) to separate fixed and per-bar cost
        base_sym_count = max(1, min(5, symbols_count // 2))
        base_sess_count = max(1, min(5, sessions_count // 2))
        if symbols_count > base_sym_count or sessions_count > base_sess_count:
            base_symbols = symbols[:base_sym_count]
            base_sessions = sessions[:base_sess_count]
            bars_0, elapsed_0, _, _ = _run_benchmark_once(
                project_dir, base_symbols, base_sessions, timeframe, with_profile=False
            )
            delta_bars = bars_1 - bars_0
            delta_elapsed = elapsed_1 - elapsed_0
            if delta_bars > 0 and delta_elapsed > 0:
                per_bar_cost = delta_elapsed / delta_bars
                fixed_cost = max(0.0, elapsed_1 - (per_bar_cost * bars_1))
            else:
                per_bar_cost = elapsed_1 / max(1, bars_1)
                fixed_cost = 0.0
        else:
            per_bar_cost = elapsed_1 / max(1, bars_1)
            fixed_cost = 0.0

        profile_text = _filter_and_format_profile(prof_1, top_n=10) if prof_1 else ""

        return BenchmarkReport(
            symbols=symbols_count,
            sessions=sessions_count,
            timeframe=timeframe,
            total_bars=bars_1,
            elapsed_seconds=elapsed_1,
            bars_per_second=bars_1 / elapsed_1,
            peak_memory_mb=peak_mb_1,
            profile_stats_text=profile_text,
            fixed_cost_seconds=fixed_cost,
            per_bar_cost_seconds=per_bar_cost,
        )
