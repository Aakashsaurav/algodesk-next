"""Tests for CP1: Replay by session, warm-up capture, refusal on multi-session days."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from algodesk.api.market import InstrumentId, Tick
from algodesk.cli.app import app
from algodesk.engine.runner import SessionRunner
from algodesk.ops.runtime import open_runtime
from algodesk.results.compare import compare_runs

cli_runner = CliRunner()
IST = ZoneInfo("Asia/Kolkata")


def _seed_instrument_cache(proj: Path) -> None:
    real_cache = (
        Path(__file__).resolve().parent.parent.parent.parent / ".local" / "cache" / "instruments"
    )
    today = datetime.now(IST).date()
    target_cache = proj / ".local" / "cache" / "instruments"
    target_cache.mkdir(parents=True, exist_ok=True)
    if real_cache.is_dir():
        for f in real_cache.glob("upstox_master_*.json.gz"):
            shutil.copyfile(f, target_cache / f"upstox_master_{today.isoformat()}.json.gz")
            break


def _make_ticks(
    instrument: str,
    start_time: datetime,
    num_ticks: int = 40,
    price_start: float = 1200.0,
) -> list[Tick]:
    inst_id = InstrumentId(instrument)
    ticks = []
    for i in range(num_ticks):
        t = start_time + timedelta(seconds=i * 2)
        price = price_start + (i * 0.5)
        ticks.append(
            Tick(
                instrument=inst_id,
                exch_ts=t,
                recv_ts=t + timedelta(milliseconds=15),
                ltp=price,
                ltq=1,
                volume=i + 1,
            )
        )
    return ticks


def test_two_fake_feed_sessions_same_day_replay_equivalence(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two paper sessions on one day each replay bit-identically to their own paper run."""
    _seed_instrument_cache(tmp_project)

    with open_runtime(tmp_project, run="demo_paper") as rt:
        t1_start = datetime(2026, 11, 4, 3, 45, 0, tzinfo=UTC)
        ticks_1 = _make_ticks("NSE:EQ:RELIANCE", t1_start, num_ticks=30, price_start=1200.0)

        t2_start = datetime(2026, 11, 4, 4, 45, 0, tzinfo=UTC)
        ticks_2 = _make_ticks("NSE:EQ:RELIANCE", t2_start, num_ticks=30, price_start=1300.0)

        # Paper session 1 with fake feed
        s1_paper_runner = SessionRunner(
            rt,
            mode="paper",
            seed=42,
            run_id="demo_paper_fast_paper_20261104_091500_111111",
        )

        async def _stream_1(c, clk):
            for tk in ticks_1:
                yield tk

        monkeypatch.setattr(s1_paper_runner, "_build_event_stream", _stream_1)
        p1 = asyncio.run(s1_paper_runner.run())
        assert p1.is_dir()
        s1_run_id = p1.name

        # Paper session 2 with fake feed
        s2_paper_runner = SessionRunner(
            rt,
            mode="paper",
            seed=42,
            run_id="demo_paper_fast_paper_20261104_101500_222222",
        )

        async def _stream_2(c, clk):
            for tk in ticks_2:
                yield tk

        monkeypatch.setattr(s2_paper_runner, "_build_event_stream", _stream_2)
        p2 = asyncio.run(s2_paper_runner.run())
        assert p2.is_dir()
        s2_run_id = p2.name

        # Replay Session 1 using --session
        s1_replay_runner = SessionRunner(
            rt,
            mode="replay",
            replay_session=s1_run_id,
            seed=42,
            run_id="demo_paper_fast_replay_s1_test",
        )
        rep1_path = asyncio.run(s1_replay_runner.run())
        assert rep1_path.is_dir()

        # Verify equivalence of Session 1
        eq1, diff1 = compare_runs(rt.root, s1_run_id, rep1_path.name)
        assert eq1, f"Session 1 replay diverged: {diff1}"

        # Replay Session 2 using --session
        s2_replay_runner = SessionRunner(
            rt,
            mode="replay",
            replay_session=s2_run_id,
            seed=42,
            run_id="demo_paper_fast_replay_s2_test",
        )
        rep2_path = asyncio.run(s2_replay_runner.run())
        assert rep2_path.is_dir()

        # Verify equivalence of Session 2
        eq2, diff2 = compare_runs(rt.root, s2_run_id, rep2_path.name)
        assert eq2, f"Session 2 replay diverged: {diff2}"


def test_replay_dates_without_session_refuses_when_multiple_sessions(tmp_project: Path) -> None:
    """Replaying a day with several recorded sessions without --session refuses with exit 1."""
    with open_runtime(tmp_project, run="demo_paper") as rt:
        results_dir = rt.paths.results
        results_dir.mkdir(parents=True, exist_ok=True)

        # Create two fake paper session results folders on 2026-11-04
        sess1 = results_dir / "demo_paper_paper_20261104_091500_aaaaaa"
        sess2 = results_dir / "demo_paper_paper_20261104_101500_bbbbbb"
        sess1.mkdir(parents=True, exist_ok=True)
        sess2.mkdir(parents=True, exist_ok=True)

        m1 = {
            "date": "2026-11-04",
            "mode": "paper",
            "first_tick_ist": "09:15:00 IST",
            "last_tick_ist": "09:45:00 IST",
            "trades": 5,
        }
        m2 = {
            "date": "2026-11-04",
            "mode": "paper",
            "first_tick_ist": "10:15:00 IST",
            "last_tick_ist": "10:45:00 IST",
            "trades": 8,
        }
        (sess1 / "metrics.json").write_text(json.dumps(m1), encoding="utf-8")
        (sess2 / "metrics.json").write_text(json.dumps(m2), encoding="utf-8")

        res = cli_runner.invoke(app, ["replay", "--dates", "2026-11-04", "--run", "demo_paper"])
        assert res.exit_code == 1
        assert (
            "multiple recorded sessions" in res.stdout.lower()
            or "refusing replay" in res.stdout.lower()
        )
        assert "demo_paper_paper_20261104_091500_aaaaaa" in res.stdout
        assert "demo_paper_paper_20261104_101500_bbbbbb" in res.stdout


def test_replay_older_run_without_warmup_parquet_reconstructs_from_store(tmp_project: Path) -> None:
    """Replay of an older run without warmup_bars.parquet reconstructs warm-up from store."""
    with open_runtime(tmp_project, run="demo_paper") as rt:
        results_dir = rt.paths.results
        sess_dir = results_dir / "demo_paper_paper_20261104_082753_old"
        sess_dir.mkdir(parents=True, exist_ok=True)

        m = {
            "date": "2026-11-04",
            "mode": "paper",
            "first_tick_utc": "2026-11-04T03:45:00+00:00",
            "last_tick_utc": "2026-11-04T04:15:00+00:00",
            "first_tick_ist": "2026-11-04 09:15:00 IST",
            "last_tick_ist": "2026-11-04 09:45:00 IST",
            "trades": 0,
        }
        (sess_dir / "metrics.json").write_text(json.dumps(m), encoding="utf-8")
        (sess_dir / "manifest.json").write_text(
            json.dumps({"seed": 7, "plugin_versions": {}}), encoding="utf-8"
        )
        (sess_dir / "SUMMARY.md").write_text(
            "Session demo_paper (paper) completed on 2026-11-04.\n", encoding="utf-8"
        )

        # Notice no warmup_bars.parquet exists in sess_dir!
        assert not (sess_dir / "warmup_bars.parquet").exists()

        runner = SessionRunner(
            rt,
            mode="replay",
            replay_session=sess_dir.name,
            seed=7,
        )
        assert runner.tick_window is not None
        assert runner.tick_window[0] == datetime(2026, 11, 4, 3, 45, tzinfo=UTC)
        assert runner.tick_window[1] == datetime(2026, 11, 4, 4, 15, tzinfo=UTC)


def test_replay_version_differences_warn_only_for_configured_plugins(tmp_project: Path) -> None:
    """Replay warns about version differences only for plugins in run config."""
    with open_runtime(tmp_project, run="demo_paper") as rt:
        results_dir = rt.paths.results
        sess_dir = results_dir / "demo_paper_paper_20261104_082753_warn"
        sess_dir.mkdir(parents=True, exist_ok=True)

        m = {
            "date": "2026-11-04",
            "mode": "paper",
            "first_tick_utc": "2026-11-04T03:45:00+00:00",
            "last_tick_utc": "2026-11-04T04:15:00+00:00",
        }
        (sess_dir / "metrics.json").write_text(json.dumps(m), encoding="utf-8")
        (sess_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "seed": 7,
                    "plugin_versions": {
                        "nse_bhavcopy": {"version": "0.0.1", "hash": "old_hash"},
                        "upstox": {"version": "0.0.1", "hash": "old_hash"},
                    },
                }
            ),
            encoding="utf-8",
        )
        # nse_bhavcopy is not in demo_paper run config, but upstox is!
        (sess_dir / "config.effective.txt").write_text(
            "data_source.plugin = 'upstox'\n", encoding="utf-8"
        )
        (sess_dir / "SUMMARY.md").write_text(
            "Session demo_paper (paper) completed on 2026-11-04.\n", encoding="utf-8"
        )

        runner = SessionRunner(
            rt,
            mode="replay",
            replay_session=sess_dir.name,
        )
        # Check warnings
        warnings = runner.version_warnings
        # Should warn about upstox, but NOT nse_bhavcopy!
        assert any("upstox" in w for w in warnings)
        assert not any("nse_bhavcopy" in w for w in warnings)
