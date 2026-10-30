"""Quarantine lifecycle management, inspection, release, and retention purging."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from algodesk.api import InstrumentId
from algodesk.api.errors import DataError
from algodesk.api.market import SeriesKey
from algodesk.api.services import JournalWriter, ParamReader
from algodesk.data.manifest import DataManifest, QuarantineRecord
from algodesk.data.schema import read_bars_parquet
from algodesk.data.store import BarStore


class QuarantineManager:
    """Manages inspection, release, and expiration purging of quarantined data."""

    __slots__ = (
        "_data_dir",
        "_journal",
        "_manifest",
        "_params",
        "_quarantine_days",
        "_store",
    )

    def __init__(
        self,
        data_dir: Path | str,
        manifest: DataManifest,
        store: BarStore | None = None,
        params: ParamReader | None = None,
        journal: JournalWriter | None = None,
        quarantine_days: int | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._manifest = manifest
        self._store = store or BarStore(data_dir=self._data_dir, manifest=self._manifest)
        self._params = params
        self._journal = journal

        if quarantine_days is not None:
            self._quarantine_days = quarantine_days
        elif params is not None:
            try:
                self._quarantine_days = int(params.get("data.quarantine_days"))
            except Exception:
                self._quarantine_days = 3
        else:
            self._quarantine_days = 3

    @property
    def quarantine_dir(self) -> Path:
        return self._data_dir / ".quarantine"

    def list_quarantined(self, status: str | None = None) -> list[QuarantineRecord]:
        """List quarantine records from manifest."""
        return self._manifest.get_quarantine(status=status)

    def show(self, id: str) -> dict[str, Any]:
        """Read and return report.json for a quarantined entry."""
        items = self._manifest.get_quarantine(id=id)
        if not items:
            raise DataError(
                what=f"Quarantine item '{id}' not found",
                where="QuarantineManager.show",
                fix="Run 'algodesk data quarantine list' to view available entries",
            )
        rep_path = self._data_dir / items[0].report_path
        if not rep_path.exists():
            return {"id": id, "reason": items[0].reason, "status": items[0].status}
        try:
            data = json.loads(rep_path.read_text())
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items()}
            return {"id": id, "data": data}
        except Exception as exc:
            return {"id": id, "error": str(exc), "reason": items[0].reason}

    def purge_expired(self, quarantine_days: int | None = None) -> int:
        """Purge all quarantine entries older than retention window."""
        days = quarantine_days if quarantine_days is not None else self._quarantine_days
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=days)

        active = self._manifest.get_quarantine(status="quarantined")
        purged_count = 0

        for item in active:
            try:
                created_dt = datetime.fromisoformat(item.created)
            except Exception:
                created_dt = now

            if created_dt < cutoff:
                # Remove disk directory if exists
                item_dir = self.quarantine_dir / item.id
                if item_dir.exists():
                    shutil.rmtree(item_dir, ignore_errors=True)

                self._manifest.update_quarantine(
                    id=item.id,
                    status="purged",
                    purged_at=now.isoformat(),
                )
                purged_count += 1

        if purged_count > 0 and self._journal is not None:
            self._journal.append(
                kind="data.quarantine",
                payload={"action": "purge_expired", "count": purged_count, "days": days},
            )

        return purged_count

    def purge(self, id: str | None = None, all_items: bool = False) -> int:
        """Purge quarantine items by id or all unpurged items."""
        # pyrefly: ignore[implicit-any-empty-container]
        targets = []
        if id:
            targets = self._manifest.get_quarantine(id=id)
        elif all_items:
            targets = [q for q in self._manifest.get_quarantine() if q.status != "purged"]

        purged_count = 0
        now_iso = datetime.now(UTC).isoformat()
        for item in targets:
            item_dir = self.quarantine_dir / item.id
            if item_dir.exists():
                shutil.rmtree(item_dir, ignore_errors=True)
            self._manifest.update_quarantine(id=item.id, status="purged", purged_at=now_iso)
            purged_count += 1

        if purged_count > 0 and self._journal is not None:
            self._journal.append(
                kind="data.quarantine",
                payload={"action": "purge_manual", "count": purged_count, "target_id": id},
            )

        return purged_count

    def release(self, id: str, reason: str) -> bool:
        """Release quarantined bars, re-validating and committing them to BarStore."""
        if not reason.strip():
            raise DataError(
                what="A valid reason must be provided to release quarantined data",
                where="QuarantineManager.release",
                fix="Provide --reason explaining why quarantined data is safe to commit",
            )

        items = self._manifest.get_quarantine(id=id)
        if not items:
            raise DataError(
                what=f"Quarantine item '{id}' not found",
                where="QuarantineManager.release",
                fix="Check available items with 'algodesk data quarantine list'",
            )

        item = items[0]
        q_dir = self.quarantine_dir / item.id
        bars_path = q_dir / "bars.parquet"
        meta_path = q_dir / "meta.json"

        if not bars_path.exists() or not meta_path.exists():
            raise DataError(
                what=f"Quarantined data files missing for '{id}'",
                where="QuarantineManager.release",
                fix=f"Ensure {bars_path} and {meta_path} exist",
            )

        meta = json.loads(meta_path.read_text())
        inst_id = InstrumentId(meta["instrument"])
        key = SeriesKey(
            instrument=inst_id,
            kind=meta["kind"],
            timeframe=meta["timeframe"],
            adjustment=meta["adjustment"],
            source=meta["source"],
        )

        bars = read_bars_parquet(bars_path, inst_id, key.timeframe)
        if not bars:
            raise DataError(
                what=f"Zero bars in quarantined file for '{id}'",
                where="QuarantineManager.release",
                fix="Cannot release empty bar set",
            )

        # Commit to store with refresh=True
        self._store.commit(key, bars, refresh=True)

        now_iso = datetime.now(UTC).isoformat()
        self._manifest.update_quarantine(id=id, status="released", released_at=now_iso)

        if self._journal is not None:
            self._journal.append(
                kind="data.quarantine",
                payload={
                    "id": id,
                    "action": "released",
                    "reason": reason,
                    "instrument": str(key.instrument),
                    "bars_count": len(bars),
                },
            )

        return True
