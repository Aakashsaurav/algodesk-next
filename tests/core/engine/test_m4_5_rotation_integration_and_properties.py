"""Integration and property tests for screen-driven rotation strategy (M4-5)."""

from __future__ import annotations

import asyncio
import csv
import json
import math
import random
import shutil
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from algodesk.api.kinds import Mode
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, InstrumentId, SeriesKey
from algodesk.api.protocols import DataSource
from algodesk.config.snapshot import build_effective
from algodesk.data.manifest import DataManifest
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime
from algodesk.results.compare import compare_runs


def _build_test_project(tmp_path: Path, proj_name: str) -> Path:
    root_dir = Path(__file__).resolve().parents[3]
    project_dir = tmp_path / proj_name
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
    return project_dir


def _make_daily_bars(
    instrument: InstrumentId,
    start_date: date,
    count: int,
    seed: int = 42,
    base_price: float = 1000.0,
    multiplier: float = 1.0,
    inst_index: int = 0,
) -> list[Bar]:
    rng = random.Random(seed)
    bars: list[Bar] = []
    price = base_price

    cur_d = start_date
    sessions_made = 0
    daily_growth = (multiplier ** (1.0 / count)) - 1.0

    while sessions_made < count:
        if cur_d.weekday() < 5:  # Monday to Friday
            phase = math.sin((sessions_made / 60.0) + (inst_index * 0.7)) * 0.003
            noise = (rng.random() - 0.48) * 0.003
            change_pct = daily_growth + phase + noise
            open_p = round(price, 2)
            close_p = round(price * (1.0 + change_pct), 2)
            high_p = round(max(open_p, close_p) * (1.0 + rng.random() * 0.002), 2)
            low_p = round(min(open_p, close_p) * (1.0 - rng.random() * 0.002), 2)
            vol = rng.randint(5000, 50000)

            s_ts = datetime(cur_d.year, cur_d.month, cur_d.day, 3, 45, tzinfo=UTC)
            e_ts = datetime(cur_d.year, cur_d.month, cur_d.day, 10, 0, tzinfo=UTC)

            bars.append(
                Bar(
                    instrument=instrument,
                    timeframe="1d",
                    start_ts=s_ts,
                    end_ts=e_ts,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=vol,
                    complete=True,
                    synthetic=False,
                )
            )
            price = close_p
            sessions_made += 1
        cur_d += timedelta(days=1)

    return bars


class _MemoryDataSource(DataSource):
    def __init__(self, all_bars: dict[InstrumentId, list[Bar]]) -> None:
        self._all_bars = all_bars
        self.manifest = PluginManifest(
            name="upstox",
            version="1.0.0",
            kind="broker",  # type: ignore[arg-type]
            api_version=1,
            entry="plugins.brokers.upstox.broker:UpstoxBroker",
            capabilities={"history": True},
            retention={"1d": 36500, "1m": 3650},
            max_span={"1d": 3650, "1m": 365},
        )

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        inst_bars = self._all_bars.get(key.instrument, [])
        return [b for b in inst_bars if start <= b.start_ts <= end]

    def instruments(self) -> Iterable[Any]:
        return []

    def stream(self, insts: Any) -> Any:
        raise NotImplementedError


def _setup_rotation_data(
    project_dir: Path,
    num_symbols: int = 12,
    num_sessions: int = 600,
    seed: int = 100,
) -> tuple[DataService, list[str], list[Bar]]:
    data_dir = project_dir / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    store = BarStore(data_dir=data_dir, manifest=manifest)

    start_date = date(2023, 1, 2)
    symbols: list[str] = [f"NSE:EQ:SYM{i:02d}" for i in range(1, num_symbols + 1)]
    multipliers = [5.0, 3.0, 2.0, 1.5, 1.2, 1.0, 0.9, 0.8, 1.1, 1.4, 2.5, 0.7]
    bars_by_inst: dict[InstrumentId, list[Bar]] = {}
    flat_bars: list[Bar] = []

    for idx, sym in enumerate(symbols):
        inst_id = InstrumentId(sym)
        mult = multipliers[idx % len(multipliers)]
        b_list = _make_daily_bars(
            inst_id,
            start_date,
            num_sessions,
            seed=seed + idx,
            multiplier=mult,
            inst_index=idx,
        )
        bars_by_inst[inst_id] = b_list
        flat_bars.extend(b_list)

    fake_src = _MemoryDataSource(bars_by_inst)
    ds = DataService(sources={"upstox": fake_src}, default_source="upstox", store=store)

    # Pre-populate Parquet store cache for each symbol
    for sym in symbols:
        inst_bars = bars_by_inst[InstrumentId(sym)]
        s_start = inst_bars[0].start_ts
        s_end = inst_bars[-1].end_ts
        ds.get(
            f"{sym}:1d",
            start=s_start,
            end=s_end,
            download="raw",
            adjust="splits",
            source="upstox",
            cache_only=False,
        )
        ds.get(
            f"{sym}:1d",
            start=s_start,
            end=s_end,
            download="adjusted",
            adjust="splits",
            source="upstox",
            cache_only=False,
        )

    # Write group definition to config/groups/demo12.toml
    grp_file = project_dir / "config" / "groups" / "demo12.toml"
    grp_file.write_text(f'name = "demo12"\nmembers = {json.dumps(symbols)}\n')

    # Ensure trend_rank_test.toml is present in project
    scr_file = project_dir / "config" / "screens" / "trend_rank_test.toml"
    scr_file.write_text(
        """name = "trend_rank_test"
description = "SMA(50) > SMA(200) ranked by ROC(126)"
timeframe = "1d"
adjust = "splits"

[[require]]
left = { ind = "sma", n = 50 }
op = ">"
right = { ind = "sma", n = 200 }

[rank]
by = { ind = "roc", n = 126 }
method = "percentile"
min = 0.0
"""
    )

    return ds, symbols, flat_bars


def _run_rotation_backtest(
    project_dir: Path,
    ds: DataService,
    run_name: str,
    top_n: int = 5,
    weight_pct: float = 15.0,
    rebalance_every: int = 21,
    start_session: date = date(2024, 1, 2),
    end_session: date = date(2025, 4, 1),
    run_id: str | None = None,
) -> Path:
    actual_run_id = run_id or run_name
    cfg_file = project_dir / "config" / "runs" / f"{run_name}.toml"
    cfg_file.write_text(
        f"""allocated_capital = 1000000.0

[run]
name = "{run_name}"
mode = "backtest"

[backtest]
start = "{start_session.isoformat()}"
end = "{end_session.isoformat()}"
fidelity = "bar"
timeframe = "1d"
study = "rotation_study"

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
group = "demo12"

[[strategies]]
plugin = "screen_rotation"
risk_profile = "conservative"
[strategies.params]
screen = "trend_rank_test"
group = "demo12"
top_n = {top_n}
weight_pct = {weight_pct}
rebalance_every = {rebalance_every}
timeframe = "1d"
product = "CNC"
"""
    )

    with open_runtime(project_dir, ensure=True) as rt:
        eff = build_effective(rt.root, run_name=run_name)
        object.__setattr__(rt, "effective", eff)
        from algodesk.engine.backtest import BacktestRunner

        bkt = BacktestRunner(
            runtime=rt,
            run_config=eff.run,
            run_name=run_name,
            run_id=actual_run_id,
            quiet=True,
        )
        bkt.session_runner._data_service = ds
        res_path = bkt.run()
        return res_path


def test_rotation_integration_run_finishes_and_rebalances(tmp_path: Path) -> None:
    """Rotation run on 12 symbols x 600 sessions finishes PASS, >= 3 rebalances."""
    proj = _build_test_project(tmp_path, "int_proj")
    ds, symbols, bars = _setup_rotation_data(proj, num_symbols=12, num_sessions=600)

    # Run 1
    res_path1 = _run_rotation_backtest(
        proj, ds, "rot_run1", top_n=5, weight_pct=15.0, rebalance_every=21
    )
    assert res_path1.is_dir()

    # Verify SUMMARY.md
    summary_text = (res_path1 / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Verdict: PASS" in summary_text
    assert "screen trend_rank_test" in summary_text

    # Verify rebalances.csv has at least 3 rebalances
    reb_csv = res_path1 / "rebalances.csv"
    assert reb_csv.is_file()
    with open(reb_csv, newline="", encoding="utf-8") as f:
        rebalances = list(csv.DictReader(f))
    assert len(rebalances) >= 3, f"Expected >= 3 rebalances, got {len(rebalances)}"

    # Check manifest.json contains screen_result_hashes
    manifest_data = json.loads((res_path1 / "manifest.json").read_text(encoding="utf-8"))
    assert "screen_result_hashes" in manifest_data
    assert len(manifest_data["screen_result_hashes"]) == len(rebalances)

    # Verify closed round trips >= 3
    metrics_data = json.loads((res_path1 / "metrics.json").read_text(encoding="utf-8"))
    assert metrics_data.get("closed_round_trips", 0) >= 3, (
        f"Expected >= 3 closed round trips, got {metrics_data.get('closed_round_trips')}"
    )

    # Check trades and position history
    trades_csv = res_path1 / "trades.csv"
    assert trades_csv.is_file()
    with open(trades_csv, newline="", encoding="utf-8") as f:
        trades = list(csv.DictReader(f))
    assert len(trades) > 0

    # Verify executed_* rows in rebalances.csv equal the fills
    executed_sells = []
    executed_buys = []
    for r in rebalances:
        if r.get("executed_sold"):
            executed_sells.extend([s for s in r["executed_sold"].split(";") if s])
        if r.get("executed_bought"):
            executed_buys.extend([s for s in r["executed_bought"].split(";") if s])

    fill_sells = [t["instrument"] for t in trades if t["side"] == "SELL"]
    fill_buys = [t["instrument"] for t in trades if t["side"] == "BUY"]
    assert len(executed_sells) == len(fill_sells)
    assert len(executed_buys) == len(fill_buys)


def test_rotation_determinism_and_compare(tmp_path: Path) -> None:
    """A second run gives equal results and equal hashes (results compare equal True)."""
    proj = _build_test_project(tmp_path, "det_proj")
    ds, symbols, bars = _setup_rotation_data(proj, num_symbols=12, num_sessions=600)

    res_path1 = _run_rotation_backtest(
        proj, ds, "rot_det", run_id="rot_det1", top_n=5, weight_pct=15.0, rebalance_every=21
    )
    res_path2 = _run_rotation_backtest(
        proj, ds, "rot_det", run_id="rot_det2", top_n=5, weight_pct=15.0, rebalance_every=21
    )

    # Compare hashes
    m1 = json.loads((res_path1 / "manifest.json").read_text(encoding="utf-8"))
    m2 = json.loads((res_path2 / "manifest.json").read_text(encoding="utf-8"))
    assert m1["result_hash"] == m2["result_hash"]
    assert m1["screen_result_hashes"] == m2["screen_result_hashes"]

    # Results compare tool
    equal, diff = compare_runs(proj, "rot_det1", "rot_det2")
    assert equal is True, f"Runs differed: {diff}"


def test_rotation_causality_garbage_after_k(tmp_path: Path) -> None:
    """Causality test: garbage after bar k leaves all decisions up to k unchanged."""
    proj = _build_test_project(tmp_path, "causal_proj")
    ds, symbols, bars = _setup_rotation_data(proj, num_symbols=12, num_sessions=600, seed=500)

    # Baseline run
    res_base = _run_rotation_backtest(
        proj, ds, "rot_base", top_n=5, weight_pct=15.0, rebalance_every=21
    )
    with open(res_base / "rebalances.csv", newline="", encoding="utf-8") as f:
        base_reb = list(csv.DictReader(f))

    assert len(base_reb) >= 3
    cutoff_date = base_reb[2]["rebalance_date"]

    # In a new project, corrupt bars strictly after cutoff_date
    proj_corrupt = _build_test_project(tmp_path, "corrupt_proj")
    ds_c, symbols_c, _ = _setup_rotation_data(
        proj_corrupt, num_symbols=12, num_sessions=600, seed=500
    )

    # Overwrite data in Parquet store after cutoff_date with wild prices
    cutoff_dt = datetime.fromisoformat(f"{cutoff_date}T23:59:59+00:00")
    for sym in symbols_c:
        raw_key = SeriesKey(InstrumentId(sym), "bars", "1d", "raw", "upstox")
        adj_key = SeriesKey(InstrumentId(sym), "bars", "1d", "adjusted", "upstox")
        sl = ds_c.get(
            raw_key,
            start=datetime(2023, 1, 1, tzinfo=UTC),
            end=datetime(2026, 12, 31, tzinfo=UTC),
            cache_only=True,
        )
        # Rewrite with corrupted bars after cutoff
        corrupted: list[Bar] = []
        for b in sl.bars:
            if b.start_ts > cutoff_dt:
                corrupted.append(
                    Bar(
                        b.instrument,
                        b.timeframe,
                        b.start_ts,
                        b.end_ts,
                        b.open * 10,
                        b.high * 10,
                        b.low * 10,
                        b.close * 10,
                        b.volume * 10,
                        True,
                        False,
                    )
                )
            else:
                corrupted.append(b)
        ds_c._store.commit(raw_key, corrupted, refresh=True)
        ds_c._store.commit(adj_key, corrupted, refresh=True)

    res_corrupt = _run_rotation_backtest(
        proj_corrupt, ds_c, "rot_corrupt", top_n=5, weight_pct=15.0, rebalance_every=21
    )
    with open(res_corrupt / "rebalances.csv", newline="", encoding="utf-8") as f:
        corrupt_reb = list(csv.DictReader(f))

    # All rebalances on or before cutoff_date must be completely identical
    for idx in range(3):
        assert corrupt_reb[idx]["rebalance_date"] == base_reb[idx]["rebalance_date"]
        assert corrupt_reb[idx]["selected_symbols"] == base_reb[idx]["selected_symbols"]
        assert corrupt_reb[idx]["intended_bought"] == base_reb[idx]["intended_bought"]
        assert corrupt_reb[idx]["executed_bought"] == base_reb[idx]["executed_bought"]
        assert corrupt_reb[idx]["intended_sold"] == base_reb[idx]["intended_sold"]
        assert corrupt_reb[idx]["executed_sold"] == base_reb[idx]["executed_sold"]


def test_rotation_properties_positions_bound_no_duplicate_buys(tmp_path: Path) -> None:
    """Property test: positions <= top_n, no BUY for held, SELL closes held position."""
    proj = _build_test_project(tmp_path, "prop_proj")
    ds, symbols, bars = _setup_rotation_data(proj, num_symbols=12, num_sessions=600)

    top_n = 5
    res_path = _run_rotation_backtest(
        proj, ds, "rot_prop", top_n=top_n, weight_pct=15.0, rebalance_every=21
    )

    # Track simulated position holding from trades.csv
    trades_csv = res_path / "trades.csv"
    with open(trades_csv, newline="", encoding="utf-8") as f:
        trades = list(csv.DictReader(f))

    # Group trades by timestamp and apply SELLs before BUYs at each rebalance
    from collections import defaultdict

    trades_by_ts: dict[str, list[dict[str, str]]] = defaultdict(list)
    for tr in trades:
        trades_by_ts[tr["ts"]].append(tr)

    current_positions: dict[str, int] = {}
    for _ts, batch in trades_by_ts.items():
        # 1. Apply SELLs first
        for tr in batch:
            if tr["side"] == "SELL":
                sym = tr["instrument"]
                qty = int(tr["qty"])
                assert current_positions.get(sym, 0) > 0, f"SELL for {sym} not held!"
                assert current_positions[sym] == qty, (
                    f"SELL qty {qty} did not close held qty {current_positions[sym]}"
                )
                current_positions[sym] = 0
        # 2. Apply BUYs second
        for tr in batch:
            if tr["side"] == "BUY":
                sym = tr["instrument"]
                qty = int(tr["qty"])
                assert current_positions.get(sym, 0) == 0, f"Duplicate BUY for {sym} already held!"
                current_positions[sym] = qty

        # Positions never exceed top_n after any fill event timestamp
        active = sum(1 for q in current_positions.values() if q > 0)
        assert active <= top_n, f"Open positions {active} exceeded top_n {top_n}!"

    # Property: cash never negative
    eq_csv = res_path / "equity_daily.csv"
    with open(eq_csv, newline="", encoding="utf-8") as f:
        eq_rows = list(csv.DictReader(f))
    for row in eq_rows:
        cash = float(row["cash"])
        assert cash >= 0.0, f"Negative cash ₹{cash} detected on {row['date']}!"


def test_rotation_risk_rejects_by_rule_counted(tmp_path: Path) -> None:
    """A run where risk engine rejects entries still ends PASS with rejects listed."""
    proj = _build_test_project(tmp_path, "risk_proj")
    ds, symbols, bars = _setup_rotation_data(proj, num_symbols=12, num_sessions=600)

    # Request weight_pct=30.0 with top_n=5 -> 5 * 30% = 150% capital
    # Binds margin_buffer_pct=25% and CashAvailableRule will reject some BUY entries
    res_path = _run_rotation_backtest(
        proj, ds, "rot_risk", top_n=5, weight_pct=30.0, rebalance_every=21
    )

    summary_text = (res_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "Verdict: PASS" in summary_text

    # Verify metrics record rejects_by_rule
    metrics_data = json.loads((res_path / "metrics.json").read_text(encoding="utf-8"))
    assert "rejects_by_rule" in metrics_data
    assert sum(metrics_data["rejects_by_rule"].values()) > 0, "Expected at least one risk rejection"
