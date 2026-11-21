"""Tests for CP1: Screen context call, ScreenService protocol, and causality."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from algodesk.api.errors import ConfigError, NotSupported, SafetyViolation
from algodesk.api.kinds import Mode
from algodesk.api.market import Bar, InstrumentId
from algodesk.api.protocols import (
    ScreenResult,
    ScreenRow,
    ScreenSelection,
    ScreenService,
    StrategyContext,
)
from algodesk.engine.clock import SimClock
from algodesk.engine.context import BarBuffer, build_strategy_context
from algodesk.engine.indicator_manager import IndicatorManager
from algodesk.ops.screens import RuntimeScreenService
from algodesk.screens.runner import ScreenRunner

IST = ZoneInfo("Asia/Kolkata")


class MockCalendar:
    def __init__(self, holidays: set[date] | None = None) -> None:
        self._holidays = holidays or set()

    def is_trading_day(self, d: date) -> bool:
        if d in self._holidays:
            return False
        return d.weekday() < 5  # Mon-Fri


class MockJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))


class MockScreenRunner:
    def __init__(self, result: ScreenResult) -> None:
        self.result = result
        self.call_count = 0
        self.last_as_of: date | None = None

    def run(self, *args: Any, **kwargs: Any) -> ScreenResult:
        self.call_count += 1
        self.last_as_of = kwargs.get("as_of")
        return self.result


def _make_dummy_result(as_of: date, syms: tuple[str, ...], res_hash: str = "hash1") -> ScreenResult:
    rows = tuple(ScreenRow(symbol=s, passed=True, score=100.0) for s in syms)
    return ScreenResult(
        name="dummy_screen",
        as_of=as_of,
        timeframe="1d",
        rows=rows,
        result_hash=res_hash,
    )


def test_screen_selection_types_and_immutability() -> None:
    sel = ScreenSelection(
        symbols=("NSE:EQ:RELIANCE", "NSE:EQ:TCS"),
        scores={"NSE:EQ:RELIANCE": 95.0, "NSE:EQ:TCS": 85.0},
        as_of=date(2024, 1, 2),
        result_hash="abc123hash",
    )
    assert sel.symbols == ("NSE:EQ:RELIANCE", "NSE:EQ:TCS")
    assert sel.scores["NSE:EQ:RELIANCE"] == 95.0
    assert sel.as_of == date(2024, 1, 2)
    assert sel.result_hash == "abc123hash"
    with pytest.raises(AttributeError):
        sel.symbols = ()  # type: ignore[misc]


def test_screen_service_protocol_conformance() -> None:
    class DummyService:
        def screen(self, name: str, *, group: str | None = None) -> ScreenSelection:
            return ScreenSelection((), {}, date(2024, 1, 1), "")

    svc = DummyService()
    assert isinstance(svc, ScreenService)


def test_strategy_context_default_raises_notsupported() -> None:
    ctx = StrategyContext()
    with pytest.raises(NotSupported) as exc_info:
        ctx.screen("test_screen")
    assert "not supported" in str(exc_info.value).lower()


def test_build_strategy_context_paper_and_live_mode_raise_notsupported() -> None:
    clock = SimClock(datetime(2024, 1, 2, 10, 0, tzinfo=UTC))
    ind_mgr = IndicatorManager()
    buf = BarBuffer()

    class DummyService:
        def screen(self, name: str, *, group: str | None = None) -> ScreenSelection:
            return ScreenSelection((), {}, date(2024, 1, 2), "hash")

    # Paper mode
    ctx_paper = build_strategy_context(
        clock=clock,
        instruments={},
        indicator_manager=ind_mgr,
        bar_buffer=buf,
        screen_service=DummyService(),
        mode=Mode.PAPER,
    )
    with pytest.raises(NotSupported) as exc_paper:
        ctx_paper.screen("test_screen")
    assert "not supported in paper mode" in str(exc_paper.value)

    # Live mode
    ctx_live = build_strategy_context(
        clock=clock,
        instruments={},
        indicator_manager=ind_mgr,
        bar_buffer=buf,
        screen_service=DummyService(),
        mode=Mode.LIVE,
    )
    with pytest.raises(NotSupported) as exc_live:
        ctx_live.screen("test_screen")
    assert "not supported in live mode" in str(exc_live.value)


def test_build_strategy_context_backtest_mode_delegates() -> None:
    clock = SimClock(datetime(2024, 1, 2, 10, 0, tzinfo=UTC))
    ind_mgr = IndicatorManager()
    buf = BarBuffer()

    expected_sel = ScreenSelection(("NSE:EQ:A",), {"NSE:EQ:A": 90.0}, date(2024, 1, 2), "hash_xyz")

    class DummyService:
        def screen(self, name: str, *, group: str | None = None) -> ScreenSelection:
            assert name == "my_screen"
            assert group == "my_group"
            return expected_sel

    ctx_bt = build_strategy_context(
        clock=clock,
        instruments={},
        indicator_manager=ind_mgr,
        bar_buffer=buf,
        screen_service=DummyService(),
        mode=Mode.BACKTEST,
    )
    sel = ctx_bt.screen("my_screen", group="my_group")
    assert sel == expected_sel


def test_runtime_screen_service_refuses_when_no_session_completed(tmp_path: Path) -> None:
    clock = SimClock(datetime(2024, 1, 2, 3, 45, tzinfo=UTC))  # 09:15 IST (session open)
    cal = MockCalendar()
    from unittest.mock import MagicMock

    rt = MagicMock()
    runner = MagicMock()

    service = RuntimeScreenService(
        runtime=rt,
        screen_runner=runner,
        clock=clock,
        calendar=cal,
        mode=Mode.BACKTEST,
        last_completed_session_fn=lambda: None,  # No session completed yet
    )

    with pytest.raises(SafetyViolation) as exc_info:
        service.screen("trend_rank_test")
    assert "No trading session completed yet" in str(exc_info.value)


def test_runtime_screen_service_journals_and_invokes_manifest_callback(tmp_path: Path) -> None:
    clock = SimClock(datetime(2024, 1, 2, 10, 0, tzinfo=UTC))  # 15:30 IST
    cal = MockCalendar()
    journal = MockJournal()
    manifest_hashes: list[str] = []

    from unittest.mock import MagicMock

    rt = MagicMock()
    rt.root = tmp_path
    cfg_screens = tmp_path / "config" / "screens"
    cfg_screens.mkdir(parents=True, exist_ok=True)
    (cfg_screens / "test_scr.toml").write_text(
        'name = "test_scr"\ntimeframe = "1d"\n'
        '[[require]]\nleft = { field = "close" }\nop = ">"\nright = 50\n'
    )
    cfg_groups = tmp_path / "config" / "groups"
    cfg_groups.mkdir(parents=True, exist_ok=True)
    (cfg_groups / "test_grp.toml").write_text('name = "test_grp"\nsymbols = ["NSE:EQ:RELIANCE"]\n')

    res = _make_dummy_result(date(2024, 1, 2), ("NSE:EQ:RELIANCE",), "res_hash_123")
    runner = MockScreenRunner(res)

    service = RuntimeScreenService(
        runtime=rt,
        screen_runner=runner,  # type: ignore[arg-type]
        clock=clock,
        calendar=cal,
        mode=Mode.BACKTEST,
        journal=journal,
        manifest_callback=lambda h: manifest_hashes.append(h),
        last_completed_session_fn=lambda: date(2024, 1, 2),
    )

    selection = service.screen("test_scr", group="test_grp")
    assert selection.symbols == ("NSE:EQ:RELIANCE",)
    assert selection.result_hash == "res_hash_123"

    # Verify journal event
    assert len(journal.events) == 1
    ev_type, payload = journal.events[0]
    assert ev_type == "universe.frozen"
    assert payload["name"] == "test_scr"
    assert payload["as_of"] == "2024-01-02"
    assert payload["hash"] == "res_hash_123"
    assert payload["count"] == 1

    # Verify manifest callback
    assert manifest_hashes == ["res_hash_123"]


def test_two_calls_on_same_bar_return_identical_results(tmp_path: Path) -> None:
    clock = SimClock(datetime(2024, 1, 2, 10, 0, tzinfo=UTC))
    cal = MockCalendar()
    from unittest.mock import MagicMock

    rt = MagicMock()
    rt.root = tmp_path
    cfg_screens = tmp_path / "config" / "screens"
    cfg_screens.mkdir(parents=True, exist_ok=True)
    (cfg_screens / "same_scr.toml").write_text(
        'name = "same_scr"\ntimeframe = "1d"\n'
        '[[require]]\nleft = { field = "close" }\nop = ">"\nright = 10\n'
    )
    cfg_groups = tmp_path / "config" / "groups"
    cfg_groups.mkdir(parents=True, exist_ok=True)
    (cfg_groups / "g.toml").write_text('name = "g"\nsymbols = ["NSE:EQ:A", "NSE:EQ:B"]\n')

    res = _make_dummy_result(date(2024, 1, 2), ("NSE:EQ:A", "NSE:EQ:B"), "hash_same")
    runner = MockScreenRunner(res)

    service = RuntimeScreenService(
        runtime=rt,
        screen_runner=runner,  # type: ignore[arg-type]
        clock=clock,
        calendar=cal,
        mode=Mode.BACKTEST,
        last_completed_session_fn=lambda: date(2024, 1, 2),
    )

    sel1 = service.screen("same_scr", group="g")
    sel2 = service.screen("same_scr", group="g")

    assert sel1 == sel2
    assert sel1.result_hash == sel2.result_hash
    assert sel1.symbols == sel2.symbols


def test_as_of_resolution_walks_back_over_holidays(tmp_path: Path) -> None:
    # Clock at 09:15 on Monday 2024-01-08
    # Saturday 2024-01-06 and Sunday 2024-01-07 are weekend
    # Friday 2024-01-05 is trading day
    clock = SimClock(datetime(2024, 1, 8, 3, 45, tzinfo=UTC))
    cal = MockCalendar()
    from unittest.mock import MagicMock

    rt = MagicMock()
    rt.root = tmp_path
    cfg_screens = tmp_path / "config" / "screens"
    cfg_screens.mkdir(parents=True, exist_ok=True)
    (cfg_screens / "test_screen.toml").write_text(
        'name = "test_screen"\ntimeframe = "1d"\n'
        '[[require]]\nleft = { field = "close" }\nop = ">"\nright = 10\n'
    )
    cfg_groups = tmp_path / "config" / "groups"
    cfg_groups.mkdir(parents=True, exist_ok=True)
    (cfg_groups / "g.toml").write_text('name = "g"\nsymbols = ["NSE:EQ:A"]\n')

    runner = MagicMock()
    service = RuntimeScreenService(
        runtime=rt,
        screen_runner=runner,
        clock=clock,
        calendar=cal,
        mode=Mode.BACKTEST,
        last_completed_session_fn=None,  # let it resolve from clock & calendar
    )

    # Should resolve to Friday 2024-01-05
    def mock_run(*args: Any, **kwargs: Any) -> ScreenResult:
        as_of = kwargs.get("as_of")
        assert as_of == date(2024, 1, 5)
        return _make_dummy_result(as_of, ("NSE:EQ:A",), "h5")

    runner.run = mock_run

    sel = service.screen("test_screen", group="g")
    assert sel.as_of == date(2024, 1, 5)


def test_runtime_screen_service_refuses_invalid_screen(tmp_path: Path) -> None:
    clock = SimClock(datetime(2024, 1, 2, 10, 0, tzinfo=UTC))
    cal = MockCalendar()
    from unittest.mock import MagicMock

    rt = MagicMock()
    rt.root = tmp_path
    rt.plugins.list.return_value = ()
    rt.plugins.create.side_effect = Exception("Not found")
    runner = MagicMock()

    service = RuntimeScreenService(
        runtime=rt,
        screen_runner=runner,
        clock=clock,
        calendar=cal,
        mode=Mode.BACKTEST,
        last_completed_session_fn=lambda: date(2024, 1, 2),
    )
    with pytest.raises(ConfigError) as exc_info:
        service.screen("non_existent_screen")
    assert "not found" in str(exc_info.value).lower()


def test_runtime_screen_service_refuses_invalid_group(tmp_path: Path) -> None:
    clock = SimClock(datetime(2024, 1, 2, 10, 0, tzinfo=UTC))
    cal = MockCalendar()
    from unittest.mock import MagicMock

    rt = MagicMock()
    rt.root = tmp_path
    cfg_screens = tmp_path / "config" / "screens"
    cfg_screens.mkdir(parents=True, exist_ok=True)
    (cfg_screens / "s.toml").write_text(
        'name = "s"\ntimeframe = "1d"\n'
        '[[require]]\nleft = { field = "close" }\nop = ">"\nright = 10\n'
    )
    cfg_groups = tmp_path / "config" / "groups"
    cfg_groups.mkdir(parents=True, exist_ok=True)
    runner = MagicMock()

    service = RuntimeScreenService(
        runtime=rt,
        screen_runner=runner,
        clock=clock,
        calendar=cal,
        mode=Mode.BACKTEST,
        last_completed_session_fn=lambda: date(2024, 1, 2),
    )
    with pytest.raises(ConfigError) as exc_info:
        service.screen("s", group="unknown_group")
    assert "group 'unknown_group' not found" in str(exc_info.value).lower()


def test_screen_runner_causality_bars_after_as_of_are_ignored(tmp_path: Path) -> None:
    """ScreenRunner ignores bars strictly after as_of (zero look-ahead causality)."""
    from algodesk.api.screens import ScreenBars
    from algodesk.screens.models import load_toml_screen

    as_of_date = date(2024, 1, 10)

    # Valid bars up to as_of
    bars_clean: list[Bar] = []
    for i in range(1, 11):
        d = date(2024, 1, i)
        s_ts = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)
        e_ts = datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC)
        bars_clean.append(
            Bar(
                InstrumentId("NSE:EQ:SYM1"),
                "1d",
                s_ts,
                e_ts,
                100.0,
                105.0,
                95.0,
                102.0,
                1000,
                True,
                False,
            )
        )

    # Same clean bars + garbage bars after as_of
    bars_with_garbage = list(bars_clean)
    for i in range(11, 20):
        d = date(2024, 1, i)
        s_ts = datetime(d.year, d.month, d.day, 3, 45, tzinfo=UTC)
        e_ts = datetime(d.year, d.month, d.day, 10, 0, tzinfo=UTC)
        bars_with_garbage.append(
            Bar(
                InstrumentId("NSE:EQ:SYM1"),
                "1d",
                s_ts,
                e_ts,
                9999.0,
                9999.0,
                9999.0,
                9999.0,
                9999,
                True,
                False,
            )
        )

    class MockDataReader:
        def __init__(self, bar_list: list[Bar]) -> None:
            self.bar_list = bar_list

        def get_bars(self, *args: Any, **kwargs: Any) -> ScreenBars:
            return ScreenBars(bars=self.bar_list, provenance={"source": "test"})

    class MockIndResolver:
        def get_indicator(self, *args: Any, **kwargs: Any) -> Any:
            return None

    cfg_file = tmp_path / "scr.toml"
    cfg_file.write_text(
        'name = "scr"\ntimeframe = "1d"\n'
        '[[require]]\nleft = { field = "close" }\nop = ">"\nright = 100.0\n'
    )
    model = load_toml_screen(cfg_file)

    runner_clean = ScreenRunner(
        data_reader=MockDataReader(bars_clean), indicator_resolver=MockIndResolver()
    )
    res_clean = runner_clean.run(model, ["NSE:EQ:SYM1"], as_of=as_of_date)

    runner_garbage = ScreenRunner(
        data_reader=MockDataReader(bars_with_garbage), indicator_resolver=MockIndResolver()
    )
    res_garbage = runner_garbage.run(model, ["NSE:EQ:SYM1"], as_of=as_of_date)

    # Decisions and scores are identical
    assert len(res_clean.rows) == 1
    assert len(res_garbage.rows) == 1
    assert res_clean.rows[0].passed is True
    assert res_garbage.rows[0].passed is True
    assert res_clean.rows[0].score == res_garbage.rows[0].score

    # But changing bars up to as_of changes the result
    bars_altered = list(bars_clean[:-1]) + [
        Bar(
            InstrumentId("NSE:EQ:SYM1"),
            "1d",
            bars_clean[-1].start_ts,
            bars_clean[-1].end_ts,
            50.0,
            50.0,
            50.0,
            50.0,
            1000,
            True,
            False,
        )
    ]
    runner_altered = ScreenRunner(
        data_reader=MockDataReader(bars_altered), indicator_resolver=MockIndResolver()
    )
    res_altered = runner_altered.run(model, ["NSE:EQ:SYM1"], as_of=as_of_date)
    assert not res_altered.rows[0].passed
