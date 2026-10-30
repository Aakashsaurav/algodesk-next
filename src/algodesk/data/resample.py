"""Pure candlestick bar resampler aligning intraday bars to exchange session open.

Conforms to M2 design:
- 1m to N-minute and hourly aligned to exchange time and session open.
- 1d to weekly (1w) and monthly (1M).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import groupby
from zoneinfo import ZoneInfo

from algodesk.api.errors import DataError
from algodesk.api.market import Bar
from algodesk.api.protocols import Calendar

_DEFAULT_EXCH_TZ = ZoneInfo("Asia/Kolkata")


def _parse_timeframe_seconds(tf: str) -> int:
    s = tf.strip().lower()
    if s.endswith("s"):
        return int(s[:-1])
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("d"):
        return int(s[:-1]) * 86400
    raise DataError(
        what=f"Cannot parse timeframe interval '{tf}'",
        where="resample._parse_timeframe_seconds",
        fix="Use standard interval like '5m', '15m', '1h', '1d', '1w', '1M'",
    )


def _aggregate_bucket(bucket: list[Bar], timeframe: str, p_start: datetime, p_end: datetime) -> Bar:
    first_b = bucket[0]
    last_b = bucket[-1]
    return Bar(
        instrument=first_b.instrument,
        timeframe=timeframe,
        start_ts=p_start,
        end_ts=p_end,
        open=first_b.open,
        high=max(b.high for b in bucket),
        low=min(b.low for b in bucket),
        close=last_b.close,
        volume=sum(b.volume for b in bucket),
        complete=True,
        synthetic=all(getattr(b, "synthetic", False) for b in bucket),
    )


def resample(
    bars: Sequence[Bar],
    timeframe: str,
    calendar: Calendar | None = None,
    exchange_tz: tzinfo | None = None,
) -> list[Bar]:
    """Pure resample function transforming 1m or 1d bars into higher timeframes."""
    if not bars:
        return []

    tf_clean = timeframe.strip()
    first_bar = bars[0]
    input_tf = first_bar.timeframe.strip().lower()
    tz = exchange_tz or _DEFAULT_EXCH_TZ

    # 1. Daily to weekly or monthly
    if input_tf == "1d":
        if tf_clean.lower() in ("1w", "w", "week", "weekly"):
            # Group by ISO year and ISO week
            res_weekly: list[Bar] = []
            for _, group in groupby(
                bars,
                key=lambda b: (
                    b.start_ts.astimezone(tz).isocalendar().year,
                    b.start_ts.astimezone(tz).isocalendar().week,
                ),
            ):
                grp_bars = list(group)
                res_weekly.append(
                    _aggregate_bucket(
                        grp_bars,
                        "1w",
                        grp_bars[0].start_ts,
                        grp_bars[-1].end_ts,
                    )
                )
            return res_weekly

        if tf_clean in ("1M", "M", "month", "monthly"):
            # Group by calendar year and month
            res_monthly: list[Bar] = []
            for _, group in groupby(
                bars,
                key=lambda b: (
                    b.start_ts.astimezone(tz).year,
                    b.start_ts.astimezone(tz).month,
                ),
            ):
                grp_bars = list(group)
                res_monthly.append(
                    _aggregate_bucket(
                        grp_bars,
                        "1M",
                        grp_bars[0].start_ts,
                        grp_bars[-1].end_ts,
                    )
                )
            return res_monthly

        raise DataError(
            what=f"Cannot resample 1d bars to '{timeframe}'",
            where="resample",
            fix="Daily bars can only be resampled to '1w' (weekly) or '1M' (monthly)",
        )

    # 2. Intraday (1m to N-minute or hourly)
    tf_sec = _parse_timeframe_seconds(timeframe)
    if tf_sec < 60:
        raise DataError(
            what=f"Target resample timeframe '{timeframe}' must be at least 1 minute",
            where="resample",
            fix="Specify target timeframe >= 1m",
        )

    # Sort strictly by start_ts
    sorted_bars = sorted(bars, key=lambda b: b.start_ts)

    # Group by trading date in exchange time
    res: list[Bar] = []
    for day, day_bars_iter in groupby(
        sorted_bars,
        key=lambda b: b.start_ts.astimezone(tz).date(),
    ):
        day_bars = list(day_bars_iter)
        if not day_bars:
            continue

        # Resolve session start anchor
        session_start: datetime | None = None
        if calendar is not None:
            sess_list = calendar.sessions(day)
            if sess_list:
                session_start = sess_list[0].start

        if session_start is None:
            # Anchor at day open: 09:15 IST for standard session or midnight
            first_local = day_bars[0].start_ts.astimezone(tz)
            if first_local.hour >= 9:
                session_start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=tz).astimezone(
                    UTC
                )
            else:
                session_start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz).astimezone(
                    UTC
                )

        # Bucket bars by session_start + k * tf_sec
        buckets: dict[int, list[Bar]] = {}
        for b in day_bars:
            diff_s = int((b.start_ts - session_start).total_seconds())
            idx = diff_s // tf_sec
            buckets.setdefault(idx, []).append(b)

        for idx in sorted(buckets.keys()):
            p_start = session_start + timedelta(seconds=idx * tf_sec)
            p_end = p_start + timedelta(seconds=tf_sec)
            res.append(_aggregate_bucket(buckets[idx], tf_clean, p_start, p_end))

    return res
