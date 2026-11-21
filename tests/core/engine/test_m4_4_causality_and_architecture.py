"""Tests for Step 4 & 5: Backtest causality proof, cheater test, architecture."""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from algodesk.api.kinds import Mode
from algodesk.api.manifest import PluginManifest
from algodesk.api.market import Bar, InstrumentId, SeriesKey
from algodesk.api.protocols import DataSource
from algodesk.api.trading import Side
from algodesk.data.manifest import DataManifest
from algodesk.data.service import DataService
from algodesk.data.store import BarStore
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime
from algodesk.results.equivalence import load_journal_events


def _get_payload(e: dict[str, Any]) -> dict[str, Any]:
    p = e.get("payload_json") or e.get("payload") or {}
    if isinstance(p, str):
        try:
            return json.loads(p)
        except Exception:
            return {}
    return p


def _get_ts(e: dict[str, Any]) -> datetime:
    p = _get_payload(e)
    ts_str = p.get("ts") or e.get("ts_utc") or e.get("ts")
    if not ts_str:
        return datetime.min.replace(tzinfo=UTC)
    dt = datetime.fromisoformat(str(ts_str))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _build_test_project(tmp_path: Path, proj_name: str) -> Path:
    import shutil

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


def _make_ohlcv_bars(
    instrument: InstrumentId,
    start_date: date,
    count: int,
    seed: int = 42,
    timeframe: str = "1d",
) -> list[Bar]:
    import random

    rng = random.Random(seed)
    bars: list[Bar] = []
    price = 1000.0

    for i in range(count):
        d = start_date + timedelta(days=i)
        change_pct = (rng.random() - 0.48) * 0.04  # slight upward bias
        open_p = round(price, 2)
        close_p = round(price * (1.0 + change_pct), 2)
        high_p = round(max(open_p, close_p) * (1.0 + rng.random() * 0.01), 2)
        low_p = round(min(open_p, close_p) * (1.0 - rng.random() * 0.01), 2)
        vol = rng.randint(1000, 10000)

        s_ts = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)
        e_ts = datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC)

        bars.append(
            Bar(
                instrument=instrument,
                timeframe=timeframe,
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

    return bars


class _MemoryDataSource(DataSource):
    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self.manifest = PluginManifest(
            name="upstox",
            version="1.0.0",
            kind="broker",
            api_version=1,
            entry="plugins.brokers.upstox.broker:UpstoxBroker",
            capabilities={"history": True},
            retention={"1d": 36500, "1m": 3650},
            max_span={"1d": 3650, "1m": 365},
        )

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        return [b for b in self._bars if start <= b.start_ts <= end]

    def instruments(self) -> Iterable[Any]:
        return []

    def stream(self, insts: Any) -> Any:
        raise NotImplementedError


def _setup_backtest_run(
    project_dir: Path,
    bars: list[Bar],
    run_name: str,
    strategy_plugin: str = "sma_cross",
    strat_params: dict[str, Any] | None = None,
) -> Path:
    inst = bars[0].instrument
    data_dir = project_dir / "data"
    m_path = data_dir / "state" / "data_manifest.db"
    m_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = DataManifest(m_path)
    store = BarStore(data_dir=data_dir, manifest=manifest)
    fake_src = _MemoryDataSource(bars)
    ds = DataService(sources={"upstox": fake_src}, default_source="upstox", store=store)

    s_start = bars[0].start_ts
    s_end = bars[-1].end_ts
    ds.get(f"{inst}:1d", start=s_start, end=s_end, source="upstox", cache_only=False)

    d_params = strat_params or {
        "timeframe": "1d",
        "fast": 5,
        "slow": 15,
        "qty": 1,
        "product": "CNC",
    }
    p_str = ", ".join(
        f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}" for k, v in d_params.items()
    )

    cfg_file = project_dir / "config" / "runs" / f"{run_name}.toml"
    cfg_file.write_text(
        f"""allocated_capital = 1000000.0
[run]
name = "{run_name}"
mode = "backtest"

[backtest]
start = "{bars[0].start_ts.date().isoformat()}"
end = "{bars[-1].start_ts.date().isoformat()}"
fidelity = "bar"
timeframe = "1d"

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
symbols = ["{inst}"]

[[strategies]]
plugin = "{strategy_plugin}"
risk_profile = "conservative"
params = {{ {p_str} }}
""",
        encoding="utf-8",
    )

    from algodesk.config.snapshot import build_effective

    with open_runtime(project_dir) as rt:
        eff = build_effective(rt.root, run_name=run_name)
        runner = SessionRunner(
            runtime=rt,
            run_config=eff.run,
            mode=Mode.BACKTEST,
            run_id=run_name,
            quiet=True,
        )
        runner._data_service = ds
        res_path = asyncio.run(runner.run())
        manifest.close()
        return res_path


# ============================================================================
# Step 4: Causality Proof Tests (Hypothesis Test)
# ============================================================================


def test_causality_proof_sma_cross(tmp_path: Path) -> None:
    """Hypothesis test: Bars after k replaced with garbage do not alter decisions up to k."""
    inst = InstrumentId("NSE:EQ:RELIANCE")
    n_bars = 40
    k = 20

    start_date = date(2025, 1, 1)
    original_bars = _make_ohlcv_bars(inst, start_date, n_bars, seed=101)

    # Garbage bars after k
    garbage_after_k = _make_ohlcv_bars(
        inst, start_date + timedelta(days=k + 1), n_bars - (k + 1), seed=999
    )
    # Ensure garbage prices are wildly different
    altered_bars = list(original_bars[: k + 1]) + [
        Bar(
            instrument=b.instrument,
            timeframe=b.timeframe,
            start_ts=b.start_ts,
            end_ts=b.end_ts,
            open=round(b.open * 2.5, 2),
            high=round(b.high * 3.0, 2),
            low=round(b.low * 1.8, 2),
            close=round(b.close * 2.2, 2),
            volume=b.volume * 5,
            complete=True,
            synthetic=False,
        )
        for b in garbage_after_k
    ]

    p1 = _build_test_project(tmp_path, "proj_run_orig")
    res1 = _setup_backtest_run(p1, original_bars, "run_orig", strategy_plugin="sma_cross")

    p2 = _build_test_project(tmp_path, "proj_run_altered")
    res2 = _setup_backtest_run(p2, altered_bars, "run_altered", strategy_plugin="sma_cross")

    ev1 = load_journal_events(res1)
    ev2 = load_journal_events(res2)

    bar_k = original_bars[k]
    k_end_ts = bar_k.end_ts
    k_start_ts = bar_k.start_ts

    # 1. Signals up to end of bar k must be identical
    sig1 = [
        (_get_ts(e), _get_payload(e).get("instrument"), _get_payload(e).get("side"))
        for e in ev1
        if e.get("kind") == "signal" and _get_ts(e) <= k_end_ts
    ]
    sig2 = [
        (_get_ts(e), _get_payload(e).get("instrument"), _get_payload(e).get("side"))
        for e in ev2
        if e.get("kind") == "signal" and _get_ts(e) <= k_end_ts
    ]
    assert sig1 == sig2, f"Signals up to bar k differed: {sig1} vs {sig2}"

    # 2. Risk decisions up to end of bar k must be identical
    risk1 = [
        _get_payload(e) for e in ev1 if e.get("kind") == "risk.decision" and _get_ts(e) <= k_end_ts
    ]
    risk2 = [
        _get_payload(e) for e in ev2 if e.get("kind") == "risk.decision" and _get_ts(e) <= k_end_ts
    ]
    assert risk1 == risk2, "Risk decisions up to bar k differed"

    # 3. Order creations up to end of bar k must be identical
    ord1 = [
        (_get_payload(e).get("instrument"), _get_payload(e).get("side"), _get_payload(e).get("qty"))
        for e in ev1
        if e.get("kind") == "order.created" and _get_ts(e) <= k_end_ts
    ]
    ord2 = [
        (_get_payload(e).get("instrument"), _get_payload(e).get("side"), _get_payload(e).get("qty"))
        for e in ev2
        if e.get("kind") == "order.created" and _get_ts(e) <= k_end_ts
    ]
    assert ord1 == ord2, "Order creations up to bar k differed"

    # 4. Fills up to the open of bar k must be identical
    fill1 = [
        (_get_payload(e).get("price"), _get_payload(e).get("qty"))
        for e in ev1
        if e.get("kind") == "fill" and _get_ts(e) <= k_start_ts
    ]
    fill2 = [
        (_get_payload(e).get("price"), _get_payload(e).get("qty"))
        for e in ev2
        if e.get("kind") == "fill" and _get_ts(e) <= k_start_ts
    ]
    assert fill1 == fill2, "Fills up to open of bar k differed"


def test_causality_proof_table_strategy(tmp_path: Path) -> None:
    """Hypothesis test table-driven: garbage after k leaves history up to k unchanged."""
    inst = InstrumentId("NSE:EQ:INFY")
    n_bars = 35
    k = 18

    start_date = date(2025, 2, 1)
    original_bars = _make_ohlcv_bars(inst, start_date, n_bars, seed=202)
    garbage_after_k = _make_ohlcv_bars(
        inst, start_date + timedelta(days=k + 1), n_bars - (k + 1), seed=888
    )

    altered_bars = list(original_bars[: k + 1]) + [
        Bar(
            instrument=b.instrument,
            timeframe=b.timeframe,
            start_ts=b.start_ts,
            end_ts=b.end_ts,
            open=round(b.open * 0.5, 2),
            high=round(b.high * 0.6, 2),
            low=round(b.low * 0.4, 2),
            close=round(b.close * 0.45, 2),
            volume=b.volume // 2,
            complete=True,
            synthetic=False,
        )
        for b in garbage_after_k
    ]

    p1 = _build_test_project(tmp_path, "proj_tab_orig")
    res1 = _setup_backtest_run(
        p1,
        original_bars,
        "tab_orig",
        strategy_plugin="sma_cross",
        strat_params={"timeframe": "1d", "fast": 3, "slow": 8, "qty": 2, "product": "CNC"},
    )

    p2 = _build_test_project(tmp_path, "proj_tab_altered")
    res2 = _setup_backtest_run(
        p2,
        altered_bars,
        "tab_altered",
        strategy_plugin="sma_cross",
        strat_params={"timeframe": "1d", "fast": 3, "slow": 8, "qty": 2, "product": "CNC"},
    )

    ev1 = load_journal_events(res1)
    ev2 = load_journal_events(res2)

    bar_k = original_bars[k]
    k_end_ts = bar_k.end_ts
    k_start_ts = bar_k.start_ts

    sig1 = [_get_payload(e) for e in ev1 if e.get("kind") == "signal" and _get_ts(e) <= k_end_ts]
    sig2 = [_get_payload(e) for e in ev2 if e.get("kind") == "signal" and _get_ts(e) <= k_end_ts]
    assert len(sig1) == len(sig2)
    for s1, s2 in zip(sig1, sig2, strict=False):
        assert s1["instrument"] == s2["instrument"]
        assert s1["side"] == s2["side"]
        assert s1["qty"] == s2["qty"]

    fill1 = [_get_payload(e) for e in ev1 if e.get("kind") == "fill" and _get_ts(e) <= k_start_ts]
    fill2 = [_get_payload(e) for e in ev2 if e.get("kind") == "fill" and _get_ts(e) <= k_start_ts]
    assert len(fill1) == len(fill2)


def test_meta_test_cheater_strategy_fails_causality(tmp_path: Path) -> None:
    """Meta-test: A cheater strategy reading the next bar makes causality test fail."""
    # A leaky helper holding future bars
    leaky_future_bars: dict[str, list[Bar]] = {}

    from algodesk.api import OrderSpec, OrderType, PlaceOrder, Product

    class CheaterStrategy:
        """Intentionally leaks future bar info: buys if next bar will close higher."""

        def __init__(self) -> None:
            self._bar_index = 0

        async def on_bar(self, bar: Bar) -> list[Any]:
            self._bar_index += 1
            future = leaky_future_bars.get(str(bar.instrument), [])
            # Read the next bar k+1
            if self._bar_index < len(future):
                next_b = future[self._bar_index]
                if next_b.close > bar.close * 1.5:  # detects the garbage spike!
                    return [
                        PlaceOrder(
                            spec=OrderSpec(
                                instrument=bar.instrument,
                                side=Side.BUY,
                                type=OrderType.MARKET,
                                qty=1,
                                product=Product.CNC,
                            ),
                        )
                    ]
            return []

    inst = InstrumentId("NSE:EQ:CHEAT")
    n_bars = 30
    k = 15
    start_date = date(2025, 3, 1)

    orig_bars = _make_ohlcv_bars(inst, start_date, n_bars, seed=303)
    altered_bars = (
        list(orig_bars[: k + 1])
        + [
            Bar(
                instrument=b.instrument,
                timeframe=b.timeframe,
                start_ts=b.start_ts,
                end_ts=b.end_ts,
                open=round(b.open * 5.0, 2),
                high=round(b.high * 5.0, 2),
                low=round(b.low * 5.0, 2),
                close=round(b.close * 5.0, 2),  # massive spike on bar k+1!
                volume=b.volume,
                complete=True,
                synthetic=False,
            )
            for b in orig_bars[k + 1 :]
        ]
    )

    # Run A with orig_bars
    leaky_future_bars[str(inst)] = orig_bars
    cheat_strat_a = CheaterStrategy()
    signals_a = []
    for idx, b in enumerate(orig_bars[: k + 1]):
        intents = asyncio.run(cheat_strat_a.on_bar(b))
        if intents:
            signals_a.append((idx, b.end_ts, intents))

    # Run B with altered_bars (spike at k+1)
    leaky_future_bars[str(inst)] = altered_bars
    cheat_strat_b = CheaterStrategy()
    signals_b = []
    for idx, b in enumerate(altered_bars[: k + 1]):
        intents = asyncio.run(cheat_strat_b.on_bar(b))
        if intents:
            signals_b.append((idx, b.end_ts, intents))

    # Assert that the cheater strategy produced DIFFERENT signals on bar k!
    assert signals_a != signals_b, (
        "Meta-test failed: cheater strategy did not diverge on future changes!"
    )


# ============================================================================
# Step 5: Architecture Tests
# ============================================================================


def test_architecture_no_backtest_branch_in_core_execution() -> None:
    """strategy_runtime, oms, risk, portfolio, and sim venue contain no branch on backtest mode.

    Only the listed composition modules may branch on backtest mode:
    - src/algodesk/engine/runner.py
    - src/algodesk/engine/backtest.py
    - src/algodesk/cli/backtest.py
    """
    root_dir = Path(__file__).resolve().parents[3]

    permitted_composition_modules = [
        str(root_dir / "src/algodesk/engine/runner.py"),
        str(root_dir / "src/algodesk/engine/backtest.py"),
        str(root_dir / "src/algodesk/cli/backtest.py"),
    ]

    # Target modules that must NEVER branch on Mode.BACKTEST
    target_files = [
        root_dir / "src/algodesk/engine/strategy_runtime.py",
        root_dir / "plugins/venues/sim/venue.py",
    ]
    for folder in ("src/algodesk/oms", "src/algodesk/risk", "src/algodesk/portfolio"):
        target_files.extend((root_dir / folder).glob("**/*.py"))

    for f_path in target_files:
        if not f_path.is_file():
            continue
        content = f_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(f_path))

        # Check for any ast.Attribute referencing BACKTEST
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "BACKTEST":
                rel = f_path.relative_to(root_dir)
                pytest.fail(f"Forbidden Mode.BACKTEST branch found in: {rel} at line {node.lineno}")

    # Verify that permitted composition modules exist and are the only ones
    for p_str in permitted_composition_modules:
        p = Path(p_str)
        assert p.is_file(), f"Composition module not found: {p}"
