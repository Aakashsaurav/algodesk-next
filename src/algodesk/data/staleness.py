"""Source-adjusted candle staleness detection and refresh (M2-4 Step 10)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo

from algodesk.api.market import ActionRecord, SeriesKey
from algodesk.api.protocols import DataSource
from algodesk.api.services import ParamReader
from algodesk.data.adjust import AdjustmentView
from algodesk.data.manifest import DataManifest
from algodesk.data.store import BarStore

_DEFAULT_EXCH_TZ = ZoneInfo("Asia/Kolkata")


def _refetch_1d_fully(
    key: SeriesKey,
    store: BarStore,
    source: DataSource,
    newest_action: date,
    manifest: DataManifest,
    ds_id: str,
) -> None:
    existing = store.read(key, start=datetime(2000, 1, 1, tzinfo=UTC), end=datetime.now(UTC))
    if existing.bars:
        new_bars = list(source.history(key, existing.bars[0].start_ts, existing.bars[-1].end_ts))
        if new_bars:
            store.commit(key, new_bars, refresh=True)
    manifest.update_actions_epoch(ds_id, newest_action.isoformat())


def _update_1m_with_audit(
    key: SeriesKey,
    store: BarStore,
    source: DataSource,
    actions: Sequence[ActionRecord],
    epoch: str | None,
    newest_action: date,
    manifest: DataManifest,
    ds_id: str,
    audit_tol: float,
    tz: tzinfo,
) -> None:
    existing = store.read(key, start=datetime(2000, 1, 1, tzinfo=UTC), end=datetime.now(UTC))
    if not existing.bars:
        manifest.update_actions_epoch(ds_id, newest_action.isoformat())
        return

    epoch_date = date.fromisoformat(epoch) if epoch else date(2000, 1, 1)
    new_actions = [a for a in actions if a.ex_date > epoch_date]
    if not new_actions:
        manifest.update_actions_epoch(ds_id, newest_action.isoformat())
        return

    updated_bars, _, _ = AdjustmentView.apply(
        bars=existing.bars,
        actions=new_actions,
        mode="total",
        exchange_tz=tz,
        accept_unverified=True,
    )

    earliest_new_ex = min(a.ex_date for a in new_actions)
    sample_bars = [b for b in existing.bars if b.start_ts.astimezone(tz).date() < earliest_new_ex]
    audit_passed = False
    if sample_bars:
        sample_end = sample_bars[-1].end_ts
        sample_start = sample_bars[max(0, len(sample_bars) - 500)].start_ts
        try:
            refetched = list(source.history(key, sample_start, sample_end))
            if refetched:
                ref_map = {b.start_ts: b.close for b in refetched}
                diffs: list[float] = [
                    abs(b.close - ref_map[b.start_ts]) / ref_map[b.start_ts] * 100.0
                    for b in updated_bars
                    if b.start_ts in ref_map and ref_map[b.start_ts] > 0.0
                ]
                if diffs and max(diffs) <= audit_tol:
                    audit_passed = True
        except Exception:
            audit_passed = False

    if audit_passed:
        store.commit(key, updated_bars, refresh=True)
    else:
        start_ts, end_ts = existing.bars[0].start_ts, existing.bars[-1].end_ts
        full_bars = list(source.history(key, start_ts, end_ts))
        if full_bars:
            store.commit(key, full_bars, refresh=True)

    manifest.update_actions_epoch(ds_id, newest_action.isoformat())


def check_and_update_adjusted_staleness(
    key: SeriesKey,
    store: BarStore,
    actions: Sequence[ActionRecord],
    sources: Mapping[str, DataSource],
    params: ParamReader | None = None,
    exchange_tz: tzinfo | None = None,
) -> bool:
    """Check if adjusted dataset is stale against actions. Refetch/multiply if needed."""
    if key.adjustment != "adjusted" or not actions:
        return False

    tz = exchange_tz or _DEFAULT_EXCH_TZ
    ds_id = store.dataset_id(key)
    manifest = store.manifest
    ds_rec = manifest.get_dataset(ds_id)
    if ds_rec is None:
        return False

    newest_action = max((a.ex_date for a in actions), default=None)
    if newest_action is None:
        return False

    epoch = ds_rec.actions_epoch
    if epoch:
        try:
            if newest_action <= date.fromisoformat(epoch):
                return False
        except Exception:
            pass

    source_plugin = sources.get(key.source)
    if not source_plugin:
        return False

    tf = key.timeframe.lower()
    audit_tol = float(params.get("corp.audit_tolerance_pct", "0.5")) if params else 0.5
    if tf == "1d":
        _refetch_1d_fully(key, store, source_plugin, newest_action, manifest, ds_id)
        return True
    if tf == "1m":
        _update_1m_with_audit(
            key, store, source_plugin, actions, epoch, newest_action, manifest, ds_id, audit_tol, tz
        )
        return True
    return False
