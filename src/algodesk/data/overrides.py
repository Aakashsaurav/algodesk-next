"""Corporate actions overrides reader and manager."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from algodesk.api.errors import DataError
from algodesk.api.market import ActionRecord

DEFAULT_OVERRIDES_PATH = Path("config/corporate_actions_overrides.toml")


@dataclass(frozen=True, slots=True)
class ActionOverride:
    """Operator override entry for a corporate action."""

    symbol: str
    ex_date: date
    type: str
    source: str
    verified_on: str
    reason: str
    exchange: str = "NSE"
    ratio: float | None = None
    ratio_old: float | None = None
    ratio_new: float | None = None
    amount: Decimal | None = None
    issue_price: Decimal | None = None

    def __post_init__(self) -> None:
        if isinstance(self.ex_date, str):
            object.__setattr__(self, "ex_date", date.fromisoformat(self.ex_date))
        for attr in ("amount", "issue_price"):
            if (val := getattr(self, attr)) is not None and not isinstance(val, Decimal):
                object.__setattr__(self, attr, Decimal(str(val)))


def _validate_override_row(raw: dict[str, Any], path: Path) -> ActionOverride:
    sym = str(raw.get("symbol", "")).strip().upper()
    ex_str = str(raw.get("ex_date", "")).strip()
    source = str(raw.get("source", "")).strip()
    v_on = str(raw.get("verified_on", "")).strip()
    reason = str(raw.get("reason", "")).strip()
    for f_name, val in (("source", source), ("verified_on", v_on), ("reason", reason)):
        if not val:
            raise DataError(
                what=f"Override entry for '{sym}' on '{ex_str}' missing required field '{f_name}'",
                where=f"load_overrides({path})",
                fix=f"Add '{f_name}' to {path}",
            )
    return ActionOverride(
        symbol=sym,
        exchange=str(raw.get("exchange", "NSE")).strip().upper(),
        ex_date=date.fromisoformat(ex_str[:10]),
        type=str(raw.get("type", "")).strip().lower(),
        source=source,
        verified_on=v_on,
        reason=reason,
        ratio=float(raw["ratio"]) if raw.get("ratio") is not None else None,
        ratio_old=float(raw["ratio_old"]) if raw.get("ratio_old") is not None else None,
        ratio_new=float(raw["ratio_new"]) if raw.get("ratio_new") is not None else None,
        amount=Decimal(str(raw["amount"])) if raw.get("amount") is not None else None,
        issue_price=(
            Decimal(str(raw["issue_price"])) if raw.get("issue_price") is not None else None
        ),
    )


def load_overrides(path: Path | str | None = None) -> list[ActionOverride]:
    """Load and validate all operator corporate action overrides from TOML."""
    target = Path(path) if path is not None else DEFAULT_OVERRIDES_PATH
    if not target.exists():
        return []

    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DataError(
            what=f"Failed to parse corporate actions overrides from {target}: {exc}",
            where="load_overrides",
            fix="Check syntax of corporate_actions_overrides.toml",
        ) from exc

    raw_list = data.get("override", [])
    if isinstance(raw_list, dict):
        raw_list = [raw_list]
    elif not isinstance(raw_list, list):
        return []

    return [_validate_override_row(item, target) for item in raw_list]


def override_to_action(ov: ActionOverride) -> ActionRecord:
    """Convert an ActionOverride into a confirmed ActionRecord."""
    return ActionRecord(
        ov.ex_date,
        ov.type,
        ov.ratio,
        ov.ratio_old,
        ov.ratio_new,
        ov.amount,
        None,
        "",
        ov.source,
        f"override:{ov.verified_on}",
        "",
        ov.symbol,
        ov.exchange,
        "confirmed",
        f"override:{ov.exchange}:{ov.symbol}:{ov.ex_date.isoformat()}:{ov.type}",
        ov.issue_price,
        ov.reason,
        ov.verified_on,
    )


def add_override(override: ActionOverride, path: Path | str | None = None) -> None:
    """Append a new validated override entry to corporate_actions_overrides.toml."""
    target = Path(path) if path is not None else DEFAULT_OVERRIDES_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "",
        "[[override]]",
        f'symbol = "{override.symbol}"',
        f'exchange = "{override.exchange}"',
        f'ex_date = "{override.ex_date.isoformat()}"',
        f'type = "{override.type}"',
        f'source = "{override.source}"',
        f'verified_on = "{override.verified_on}"',
        f'reason = "{override.reason}"',
    ]
    for k in ("ratio", "ratio_old", "ratio_new"):
        if (v := getattr(override, k)) is not None:
            lines.append(f"{k} = {v}")
    for k in ("amount", "issue_price"):
        if (v := getattr(override, k)) is not None:
            lines.append(f'{k} = "{v}"')
    with open(target, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
