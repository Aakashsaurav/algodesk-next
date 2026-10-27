"""Persistent kill-switch with atomic state updates and instantaneous cross-process sync.

Controls trading halts across the workbench.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from algodesk.api.protocols import Clock, SystemClock
from algodesk.api.services import JournalWriter


class KillSwitch:
    """Persistent kill-switch state manager with atomic writes and mtime sync."""

    def __init__(
        self,
        state_file: Path | str | None = None,
        journal: JournalWriter | Any | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.state_file = Path(state_file or "data/state/killswitch.json")
        self.journal = journal
        self.clock = clock or SystemClock()
        self._last_mtime: int | None = None
        self._cached_state: dict[str, Any] = {"active": False, "reason": "", "by": "", "ts": None}
        self._sync()

    def _sync(self) -> None:
        """Check file mtime on disk and reload if updated by another process."""
        if not self.state_file.exists():
            self._last_mtime = None
            self._cached_state = {"active": False, "reason": "", "by": "", "ts": None}
            return

        try:
            st = os.stat(self.state_file)
            mtime = st.st_mtime_ns
            if mtime != self._last_mtime:
                content = self.state_file.read_text(encoding="utf-8")
                self._cached_state = json.loads(content)
                self._last_mtime = mtime
        except Exception:
            # If reading or stat failed, keep existing cache
            pass

    def _write_atomic(self, data: dict[str, Any]) -> None:
        """Write JSON atomically to disk via temporary file rename."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=2)

        dir_path = self.state_file.parent
        with tempfile.NamedTemporaryFile("w", dir=dir_path, delete=False, encoding="utf-8") as tf:
            tf.write(content)
            tf.flush()
            os.fsync(tf.fileno())
            temp_name = tf.name

        os.replace(temp_name, self.state_file)
        self._cached_state = data
        try:
            self._last_mtime = os.stat(self.state_file).st_mtime_ns
        except Exception:
            self._last_mtime = None

    def is_active(self) -> bool:
        """Check if kill-switch is active, syncing with disk on every call."""
        self._sync()
        return bool(self._cached_state.get("active", False))

    def status(self) -> dict[str, Any]:
        """Return the current kill-switch state dictionary."""
        self._sync()
        return dict(self._cached_state)

    def trip(self, reason: str, by: str = "manual") -> None:
        """Trip the kill-switch and record durable journal event."""
        now_ts = self.clock.now()
        data = {
            "active": True,
            "reason": reason,
            "by": by,
            "ts": now_ts.isoformat(),
        }
        self._write_atomic(data)

        if self.journal is not None and hasattr(self.journal, "append"):
            self.journal.append(
                "killswitch.change",
                {
                    "active": True,
                    "reason": reason,
                    "by": by,
                    "ts": now_ts.isoformat(),
                },
                durable=True,
            )

    def clear(self, reason: str, by: str = "manual") -> None:
        """Clear the kill-switch and record durable journal event."""
        now_ts = self.clock.now()
        data = {
            "active": False,
            "reason": reason,
            "by": by,
            "ts": now_ts.isoformat(),
        }
        self._write_atomic(data)

        if self.journal is not None and hasattr(self.journal, "append"):
            self.journal.append(
                "killswitch.change",
                {
                    "active": False,
                    "reason": reason,
                    "by": by,
                    "ts": now_ts.isoformat(),
                },
                durable=True,
            )
