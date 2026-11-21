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


def run_benchmark_measurement(
    symbols_count: int = 100,
    sessions_count: int = 250,
    timeframe: str = "1d",
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
        start_date = date(2025, 1, 1)
        sessions: list[date] = []
        cur_d = start_date
        while len(sessions) < sessions_count:
            if cur_d.weekday() < 5:
                sessions.append(cur_d)
            cur_d += timedelta(days=1)

        bars_by_symbol: dict[InstrumentId, list[Bar]] = {}
        total_bars_count = 0

        if timeframe == "1d":
            for idx, sym in enumerate(symbols):
                sym_bars: list[Bar] = []
                base_price = 100.0 + (idx % 20) * 10.0
                for s_idx, d in enumerate(sessions):
                    factor = 1.0 + 0.10 * math.sin(s_idx * 2 * math.pi / 30.0)
                    c_price = round(base_price * factor, 2)
                    o_price = round(
                        base_price * (1.0 + 0.10 * math.sin((s_idx - 0.5) * 2 * math.pi / 30.0)), 2
                    )
                    h_price = round(max(o_price, c_price) * 1.01, 2)
                    l_price = round(min(o_price, c_price) * 0.99, 2)
                    start_ts = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)
                    end_ts = datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC)
                    bar = Bar(
                        instrument=sym,
                        timeframe="1d",
                        start_ts=start_ts,
                        end_ts=end_ts,
                        open=o_price,
                        high=h_price,
                        low=l_price,
                        close=c_price,
                        volume=10000,
                        complete=True,
                        synthetic=False,
                    )
                    sym_bars.append(bar)
                bars_by_symbol[sym] = sym_bars
                total_bars_count += len(sym_bars)
        else:
            # 1m timeframe: intraday 375 bars per session (03:45 to 10:00 UTC)
            bars_per_sess = 375
            for idx, sym in enumerate(symbols):
                sym_bars = []
                base_price = 100.0 + (idx % 20) * 10.0
                for s_idx, d in enumerate(sessions):
                    s_open = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)
                    for m_idx in range(bars_per_sess):
                        b_start = s_open + timedelta(minutes=m_idx)
                        b_end = b_start + timedelta(minutes=1)
                        step = s_idx * bars_per_sess + m_idx
                        factor = 1.0 + 0.05 * math.sin(step * 2 * math.pi / 100.0)
                        c_price = round(base_price * factor, 2)
                        o_price = round(c_price * 0.999, 2)
                        h_price = round(max(o_price, c_price) * 1.001, 2)
                        l_price = round(min(o_price, c_price) * 0.999, 2)
                        bar = Bar(
                            instrument=sym,
                            timeframe="1m",
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
                        sym_bars.append(bar)
                bars_by_symbol[sym] = sym_bars
                total_bars_count += len(sym_bars)

        data_dir = project_dir / "data"
        m_path = data_dir / "state" / "data_manifest.db"
        m_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = DataManifest(m_path)
        store = BarStore(data_dir=data_dir, manifest=manifest)

        class FakeSource(DataSource):
            def __init__(self, b_map: dict[InstrumentId, list[Bar]]) -> None:
                self.b_map = b_map
                self.manifest = PluginManifest(
                    name="upstox",
                    version="1.0.0",
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

        fake_src = FakeSource(bars_by_symbol)
        ds = DataService(sources={"upstox": fake_src}, default_source="upstox", store=store)
        s_start = datetime(sessions[0].year, sessions[0].month, sessions[0].day, 0, 0, tzinfo=UTC)
        s_end = datetime(
            sessions[-1].year, sessions[-1].month, sessions[-1].day, 23, 59, tzinfo=UTC
        )
        for sym in symbols:
            ds.get(
                f"{sym}:{timeframe}",
                start=s_start,
                end=s_end,
                source="upstox",
                cache_only=False,
            )

        # Configure run
        cfg_file = project_dir / "config" / "runs" / f"bench_{symbols_count}x{sessions_count}.toml"
        sym_list_str = ", ".join(f'"{s}"' for s in symbols)
        cfg_content = f"""allocated_capital = 10000000.0

[run]
name = "bench_{symbols_count}x{sessions_count}"
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

        bench_name = f"bench_{symbols_count}x{sessions_count}"
        with open_runtime(project_dir, run=bench_name, ensure=True) as rt:
            runner = SessionRunner(
                runtime=rt,
                run_config=rt.effective.run,
                mode=Mode.BACKTEST,
                quiet=True,
            )
            runner._data_service = ds

            import asyncio

            tracemalloc.start()
            prof = cProfile.Profile()
            t0 = time.perf_counter()
            prof.enable()

            asyncio.run(runner.run())

            prof.disable()
            t1 = time.perf_counter()
            _, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            elapsed = max(0.001, t1 - t0)
            bars_per_sec = total_bars_count / elapsed
            peak_mb = peak_bytes / (1024 * 1024)

            s_buf = io.StringIO()
            ps = pstats.Stats(prof, stream=s_buf).sort_stats("cumulative")
            ps.print_stats(10)
            profile_text = s_buf.getvalue()

            manifest.close()

            return BenchmarkReport(
                symbols=symbols_count,
                sessions=sessions_count,
                timeframe=timeframe,
                total_bars=total_bars_count,
                elapsed_seconds=elapsed,
                bars_per_second=bars_per_sec,
                peak_memory_mb=peak_mb,
                profile_stats_text=profile_text,
            )
