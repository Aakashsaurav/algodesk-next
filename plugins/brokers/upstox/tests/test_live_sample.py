"""Tests verifying live sample feed frames recorded from Upstox Market Data Feed."""

from __future__ import annotations

import ast
import datetime
from pathlib import Path

from algodesk.api import FeedStatus, Level, Quote, Tick

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "live_sample"

_SAFE_GLOBALS: dict[str, object] = {
    "FeedStatus": FeedStatus,
    "Tick": Tick,
    "Quote": Quote,
    "Level": Level,
    "datetime": datetime,
}


def _eval_ast_node(node: ast.AST) -> object:
    if isinstance(node, ast.Expression):
        return _eval_ast_node(node.body)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Tuple):
        return tuple(_eval_ast_node(elt) for elt in node.elts)
    if isinstance(node, ast.List):
        return [_eval_ast_node(elt) for elt in node.elts]
    if isinstance(node, ast.Name):
        if node.id == "None":
            return None
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        if node.id in _SAFE_GLOBALS:
            return _SAFE_GLOBALS[node.id]
        raise ValueError(f"Unknown name in ast: {node.id}")
    if isinstance(node, ast.Attribute):
        val = _eval_ast_node(node.value)
        return getattr(val, node.attr)
    if isinstance(node, ast.Call):
        func = _eval_ast_node(node.func)
        args = [_eval_ast_node(a) for a in node.args]
        kwargs = {kw.arg: _eval_ast_node(kw.value) for kw in node.keywords if kw.arg is not None}
        if callable(func):
            return func(*args, **kwargs)
        raise TypeError(f"Target {func} is not callable")
    raise TypeError(f"Unsupported AST node: {type(node)}")


def _decode_event_file(path: Path) -> object:
    content = path.read_text(encoding="utf-8").strip()
    parsed = ast.parse(content, mode="eval")
    return _eval_ast_node(parsed)


def test_live_sample_frames_exist_and_bounded_size() -> None:
    files = sorted(FIXTURES_DIR.glob("event_*.txt"))
    assert len(files) >= 15, "Expected at least 15 recorded live sample events"

    total_bytes = sum(f.stat().st_size for f in files)
    assert total_bytes <= 200 * 1024, f"Live sample fixtures {total_bytes} exceeded 200 KB cap"

    readme = FIXTURES_DIR / "README.md"
    assert readme.is_file(), "README.md must be present in live_sample"
    readme_text = readme.read_text(encoding="utf-8")
    assert "Upstox" in readme_text
    assert "market data only" in readme_text.lower()


def test_live_sample_decode_and_invariants() -> None:
    files = sorted(FIXTURES_DIR.glob("event_*.txt"))
    events: list[object] = [_decode_event_file(f) for f in files]

    last_recv_ts: datetime.datetime | None = None
    tick_count = 0
    quote_count = 0

    for ev in events:
        if isinstance(ev, FeedStatus):
            assert ev.state in ("connected", "disconnected", "gap")
            assert ev.ts.tzinfo is not None
        elif isinstance(ev, Tick):
            tick_count += 1
            assert ev.exch_ts.tzinfo is not None
            assert ev.recv_ts.tzinfo is not None
            # No negative prices or quantities
            assert ev.ltp >= 0, f"Negative LTP in {ev}"
            assert ev.ltq >= 0, f"Negative LTQ in {ev}"
            assert ev.volume >= 0, f"Negative volume in {ev}"
            if ev.oi is not None:
                assert ev.oi >= 0, f"Negative OI in {ev}"

            # Monotonic arrival ordering
            if last_recv_ts is not None:
                assert ev.recv_ts >= last_recv_ts
            last_recv_ts = ev.recv_ts

        elif isinstance(ev, Quote):
            quote_count += 1
            assert ev.exch_ts.tzinfo is not None
            assert ev.recv_ts.tzinfo is not None
            assert ev.ltp >= 0, f"Negative LTP in quote {ev}"
            assert ev.volume >= 0, f"Negative volume in quote {ev}"

            # Depth levels sorted
            # Bids: descending by price
            for i in range(len(ev.bids) - 1):
                assert ev.bids[i].price >= ev.bids[i + 1].price, (
                    f"Bids not sorted descending in {ev.bids}"
                )
                assert ev.bids[i].price >= 0, f"Negative bid price {ev.bids[i]}"
                assert ev.bids[i].qty >= 0, f"Negative bid qty {ev.bids[i]}"

            # Asks: ascending by price
            for i in range(len(ev.asks) - 1):
                assert ev.asks[i].price <= ev.asks[i + 1].price, (
                    f"Asks not sorted ascending in {ev.asks}"
                )
                assert ev.asks[i].price >= 0, f"Negative ask price {ev.asks[i]}"
                assert ev.asks[i].qty >= 0, f"Negative ask qty {ev.asks[i]}"

            # Best bid must be <= best ask
            if ev.bids and ev.asks:
                assert ev.bids[0].price <= ev.asks[0].price, (
                    f"Crossed market: best bid {ev.bids[0].price} > best ask {ev.asks[0].price}"
                )

            # Monotonic arrival ordering
            if last_recv_ts is not None:
                assert ev.recv_ts >= last_recv_ts
            last_recv_ts = ev.recv_ts
        else:
            raise TypeError(f"Unexpected event type: {type(ev)}")

    assert tick_count > 0, "Expected at least one Tick in live sample"
    assert quote_count > 0, "Expected at least one Quote in live sample"
