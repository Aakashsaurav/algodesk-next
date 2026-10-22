"""Central parameter registry implementing ParamReader with audit trail."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from algodesk.api.errors import ParamError
from algodesk.api.kinds import Mode
from algodesk.api.services import ParamReader, ParamValue
from algodesk.params.defaults import load_defaults as load_defaults_file
from algodesk.params.specs import ParamSpec, Tighten, validate_value
from algodesk.params.store import ParamStore


def _utcnow() -> datetime:
    """Return timezone-aware current UTC time."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class ChangeResult:
    """Outcome of a parameter modification request."""

    name: str
    old_value: Any
    new_value: Any
    direction: str
    status: str
    effective_at: str
    scope: str = "global"
    by: str = ""
    reason: str = ""

    def __repr__(self) -> str:
        return (
            f"ChangeResult(name={self.name!r}, old_value={self.old_value!r}, "
            f"new_value={self.new_value!r}, direction={self.direction!r}, "
            f"status={self.status!r}, effective_at={self.effective_at!r})"
        )

    def __str__(self) -> str:
        return (
            f"ChangeResult({self.name}: {self.old_value} -> {self.new_value}, "
            f"{self.direction}, {self.status} {self.effective_at})"
        )


@dataclass(frozen=True)
class ParamRow:
    """Summary of parameter status for display."""

    name: str
    value: Any
    default: Any
    scope: str
    last_change: str | None


@dataclass(frozen=True)
class HistoryEntry:
    """Single audit record in parameter change history."""

    id: int
    ts_utc: str
    name: str
    scope: str
    old_value: Any
    new_value: Any
    by: str
    channel: str
    reason: str
    confirmed: str | None
    status: str


def _calculate_direction(spec: ParamSpec, current_val: Any, new_val: Any) -> str:
    """Calculate whether the change is tighten, loosen, or neutral."""
    if spec.tighten == Tighten.NONE or current_val == new_val:
        return "neutral"
    if spec.tighten == Tighten.DOWN:
        return "tighten" if new_val < current_val else "loosen"
    if spec.tighten == Tighten.UP:
        return "tighten" if new_val > current_val else "loosen"
    return "neutral"


class ParamRegistry(ParamReader):
    """Central parameter registry holding specs, defaults, and overrides."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        mode: Mode = Mode.PAPER,
        armed: Callable[[], bool] = lambda: False,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = ParamStore(db_path)
        self._mode = mode
        self._armed = armed
        self._clock = clock
        self._subscribers: set[Callable[[str, Any], None]] = set()

        self._specs: dict[str, ParamSpec] = {}
        for s_json in self._store.load_all_specs_json().values():
            spec = ParamSpec.model_validate_json(s_json)
            self._specs[spec.name] = spec

        self._process_specs: dict[str, ParamSpec] = {}
        self._overrides = self._store.load_all_values()
        self._local_change_seq = self._store.get_change_seq()
        self._cached_effective: dict[str, Any] = {name: self.get(name) for name in self._specs}

    def close(self) -> None:
        """Close backing store."""
        self._store.close()

    def __enter__(self) -> ParamRegistry:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def spec(self, name: str) -> ParamSpec:
        """Return registered parameter specification or raise ParamError."""
        s = self._specs.get(name)
        if s is None:
            raise ParamError(
                what=f"Unknown parameter '{name}'",
                where=name,
                fix="Register parameter spec before accessing specification",
            )
        return s

    def register(self, spec: ParamSpec) -> None:
        """Register a parameter specification. Idempotent if identical."""
        existing = self._process_specs.get(spec.name)
        if existing is not None:
            if existing == spec:
                return
            raise ParamError(
                what=f"Conflicting specification for parameter '{spec.name}'",
                where=spec.name,
                fix="Ensure all registrations for this parameter share the same definition",
            )
        self._process_specs[spec.name] = spec
        self._specs[spec.name] = spec
        self._store.save_spec_json(spec.name, spec.model_dump_json())
        self._cached_effective[spec.name] = self.get(spec.name)

    def load_defaults(self, dir_path: Path | str) -> list[ParamSpec]:
        """Load default specifications from TOML files and upsert authoritative specs."""
        specs = load_defaults_file(dir_path)
        for s in specs:
            self._process_specs[s.name] = s
            self._specs[s.name] = s
            self._store.save_spec_json(s.name, s.model_dump_json())
            self._reconcile_override_bounds(s)
            self._cached_effective[s.name] = self.get(s.name)
        return specs

    def _reconcile_override_bounds(self, spec: ParamSpec) -> None:
        """Invalidate stored overrides that violate updated specification bounds."""
        for (name, scope), (val, _) in list(self._overrides.items()):
            if name != spec.name:
                continue
            try:
                validate_value(spec, val)
            except Exception as exc:
                ts_utc = self._clock().isoformat()
                with self._store.transaction():
                    self._store.delete_value(spec.name, scope)
                    self._store.add_history(
                        ts_utc=ts_utc,
                        name=spec.name,
                        scope=scope,
                        old_value=val,
                        new_value=spec.default,
                        by="registry",
                        channel="validation",
                        reason=f"Override invalidated: violates new bounds ({exc})",
                        confirmed=None,
                        status="invalidated",
                    )
                self._overrides.pop((spec.name, scope), None)

    def get(self, name: str, scope: str | None = None) -> ParamValue:
        """Resolve parameter value: scoped override -> global override -> default."""
        spec = self._specs.get(name)
        if spec is None:
            raise ParamError(
                what=f"Unknown parameter '{name}'",
                where=name,
                fix="Register parameter spec before accessing value",
            )
        if scope is not None and (name, scope) in self._overrides:
            return self._overrides[(name, scope)][0]  # type: ignore[no-any-return]
        if (name, "global") in self._overrides:
            return self._overrides[(name, "global")][0]  # type: ignore[no-any-return]
        return spec.default  # type: ignore[no-any-return]

    def _validate_name_and_reason(self, name: str, reason: str) -> ParamSpec:
        """Validate parameter exists and reason is at least 5 non-whitespace characters."""
        spec = self._specs.get(name)
        if spec is None:
            raise ParamError(
                what=f"Cannot operate on unknown parameter '{name}'",
                where=name,
                fix="Register parameter spec before operating on it",
            )
        if not reason or len(reason.strip()) < 5:
            raise ParamError(
                what="Reason is mandatory and must be at least 5 characters",
                where=name,
                fix="Provide a descriptive reason (at least 5 characters)",
            )
        return spec

    def _validate_change_request(self, name: str, reason: str, value: Any) -> tuple[ParamSpec, Any]:
        """Verify parameter exists, reason is valid, and value satisfies spec."""
        spec = self._validate_name_and_reason(name, reason)
        valid_value = validate_value(spec, value)
        return spec, valid_value

    def _stage_pending(
        self,
        name: str,
        scope: str,
        valid_value: Any,
        current_effective: Any,
        direction: str,
        by: str,
        reason: str,
        channel: str,
        confirm: str | None,
        ts_utc: str,
    ) -> ChangeResult:
        """Stage a pending parameter change when armed in live mode."""
        expected_confirm = f"LOOSEN {name}"
        if confirm != expected_confirm:
            raise ParamError(
                what=f"Loosening '{name}' while armed in LIVE mode requires typed confirmation",
                where=name,
                fix=f"Pass confirm={expected_confirm!r} to proceed",
            )
        with self._store.transaction():
            self._store.add_pending(name, scope, valid_value, ts_utc, "session_start")
            self._store.add_history(
                ts_utc=ts_utc,
                name=name,
                scope=scope,
                old_value=current_effective,
                new_value=valid_value,
                by=by,
                channel=channel,
                reason=reason,
                confirmed=confirm,
                status="pending",
            )
        return ChangeResult(
            name=name,
            old_value=current_effective,
            new_value=valid_value,
            direction=direction,
            status="pending",
            effective_at="session_start",
            scope=scope,
            by=by,
            reason=reason,
        )

    def _apply_immediate(
        self,
        name: str,
        scope: str,
        valid_value: Any,
        current_effective: Any,
        direction: str,
        by: str,
        reason: str,
        channel: str,
        confirm: str | None,
        ts_utc: str,
    ) -> ChangeResult:
        """Apply a parameter change immediately to store and local overrides."""
        with self._store.transaction():
            self._store.set_value(name, scope, valid_value, ts_utc)
            self._store.add_history(
                ts_utc=ts_utc,
                name=name,
                scope=scope,
                old_value=current_effective,
                new_value=valid_value,
                by=by,
                channel=channel,
                reason=reason,
                confirmed=confirm,
                status="applied",
            )
        self._overrides[(name, scope)] = (valid_value, ts_utc)
        return ChangeResult(
            name=name,
            old_value=current_effective,
            new_value=valid_value,
            direction=direction,
            status="applied",
            effective_at="now",
            scope=scope,
            by=by,
            reason=reason,
        )

    def set(
        self,
        name: str,
        value: Any,
        *,
        by: str,
        reason: str,
        channel: str = "cli",
        scope: str = "global",
        confirm: str | None = None,
    ) -> ChangeResult:
        """Modify parameter value according to change and live-gating rules."""
        spec, valid_value = self._validate_change_request(name, reason, value)
        current_effective = self.get(name, scope=scope)
        direction = _calculate_direction(spec, current_effective, valid_value)
        ts_utc = self._clock().isoformat()

        is_armed_live = self._mode == Mode.LIVE and self._armed()
        if is_armed_live and direction == "loosen":
            return self._stage_pending(
                name,
                scope,
                valid_value,
                current_effective,
                direction,
                by,
                reason,
                channel,
                confirm,
                ts_utc,
            )

        return self._apply_immediate(
            name,
            scope,
            valid_value,
            current_effective,
            direction,
            by,
            reason,
            channel,
            confirm,
            ts_utc,
        )

    def reset(
        self,
        name: str,
        *,
        by: str,
        reason: str,
        channel: str = "cli",
        scope: str = "global",
    ) -> ChangeResult:
        """Reset parameter override back to the default specification."""
        spec = self._validate_name_and_reason(name, reason)
        current_effective = self.get(name, scope=scope)
        direction = _calculate_direction(spec, current_effective, spec.default)
        ts_utc = self._clock().isoformat()

        with self._store.transaction():
            self._store.delete_value(name, scope)
            self._store.add_history(
                ts_utc=ts_utc,
                name=name,
                scope=scope,
                old_value=current_effective,
                new_value=spec.default,
                by=by,
                channel=channel,
                reason=reason,
                confirmed=None,
                status="applied",
            )
        self._overrides.pop((name, scope), None)
        return ChangeResult(
            name=name,
            old_value=current_effective,
            new_value=spec.default,
            direction=direction,
            status="applied",
            effective_at="now",
            scope=scope,
            by=by,
            reason=reason,
        )

    def subscribe(self, callback: Callable[[str, Any], None]) -> Callable[[], None]:
        """Subscribe to parameter value updates dispatched at safe points."""
        self._subscribers.add(callback)

        def _unsubscribe() -> None:
            self._subscribers.discard(callback)

        return _unsubscribe

    def safe_point(self) -> int:
        """Re-read database if change_seq moved, apply updates, and notify subscribers."""
        db_seq = self._store.get_change_seq()
        if db_seq != self._local_change_seq:
            self._overrides = self._store.load_all_values()
            self._local_change_seq = db_seq
            specs_json = self._store.load_all_specs_json()
            for s_name, s_val in specs_json.items():
                if s_name not in self._specs:
                    self._specs[s_name] = ParamSpec.model_validate_json(s_val)

        changed: list[tuple[str, Any]] = []
        for name in sorted(self._specs.keys()):
            new_eff = self.get(name)
            old_eff = self._cached_effective.get(name)
            if old_eff != new_eff:
                changed.append((name, new_eff))
                self._cached_effective[name] = new_eff

        for name, val in changed:
            for cb in list(self._subscribers):
                try:
                    cb(name, val)
                except Exception:
                    pass

        return len(changed)

    def session_start(self) -> int:
        """Apply all pending loosenings at session start."""
        pending_rows = self._store.load_all_pending()
        if not pending_rows:
            return 0

        ts_utc = self._clock().isoformat()
        applied_count = 0
        with self._store.transaction():
            for row in pending_rows:
                name = str(row["name"])
                scope = str(row["scope"])
                val = row["value"]
                cur = self.get(name, scope=scope)
                self._store.set_value(name, scope, val, ts_utc)
                self._store.add_history(
                    ts_utc=ts_utc,
                    name=name,
                    scope=scope,
                    old_value=cur,
                    new_value=val,
                    by="session_start",
                    channel="lifecycle",
                    reason="Applied pending loosening at session start",
                    confirmed=None,
                    status="applied",
                )
                applied_count += 1

        self.safe_point()
        return applied_count

    def history(self, name: str | None = None, limit: int = 50) -> list[HistoryEntry]:
        """Fetch audit history for parameters."""
        raw_entries = self._store.query_history(name=name, limit=limit)
        return [HistoryEntry(**entry) for entry in raw_entries]

    def show(self, prefix: str = "") -> list[ParamRow]:
        """List parameters with effective value, default, scope, and last change time."""
        rows: list[ParamRow] = []
        for name in sorted(self._specs.keys()):
            if not name.startswith(prefix):
                continue
            spec = self._specs[name]
            eff_val = self.get(name)
            hist = self._store.query_history(name=name, limit=1)
            last_change = hist[0]["ts_utc"] if hist else None
            rows.append(
                ParamRow(
                    name=name,
                    value=eff_val,
                    default=spec.default,
                    scope=spec.scope,
                    last_change=last_change,
                )
            )
        return rows

    def snapshot(self) -> tuple[str, dict[str, Any]]:
        """Return canonical SHA-256 hash and effective dictionary of all registered parameters."""
        values: dict[str, Any] = {name: self.get(name) for name in sorted(self._specs.keys())}
        for (name, scope), (val, _) in sorted(self._overrides.items()):
            values[f"{name}@{scope}"] = val
        canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return digest, values
