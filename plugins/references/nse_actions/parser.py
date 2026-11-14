"""Deterministic parser for NSE corporate action purpose texts into ActionRecords."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
import tomllib
from typing import Any

from algodesk.api.market import ActionRecord

SPLIT_RE = re.compile(
    r"(?:split|sub-?\s*division).*?(?:from|of)\s*(?:rs\.?|re\.?)?\s*([0-9]+(?:\.[0-9]+)?)"
    r"\s*(?:/-)?\s*(?:per\s+(?:equity\s+)?sh(?:are)?)?\s*to\s*(?:rs\.?|re\.?)?\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*(?:/-)?\s*(?:per\s+(?:equity\s+)?sh(?:are)?)?",
    re.IGNORECASE,
)

BONUS_RE = re.compile(
    r"bonus\s*([0-9]+(?:\.[0-9]+)?)\s*[:/]\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

RIGHTS_RE = re.compile(
    r"rights?\s*([0-9]+(?:\.[0-9]+)?)\s*[:/]\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

RIGHTS_PRICE_RE = re.compile(
    r"@\s*(?:(?:issue\s+)?price\s+)?(?:rs\.?|re\.?)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/-)?\s*"
    r"(?:per\s+(?:equity\s+)?sh(?:are)?)?",
    re.IGNORECASE,
)

DIV_AMT_RE = re.compile(
    r"(?:rs\.?|re\.?)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/-)?\s*(?:per\s+(?:equity\s+)?sh(?:are)?)?"
    r"|([0-9]+(?:\.[0-9]+)?)\s*(?:/-)?\s*per\s+(?:equity\s+)?sh(?:are)?",
    re.IGNORECASE,
)


def _parse_nse_date(val: Any) -> date | None:
    if not val or not isinstance(val, str) or val.strip() in ("-", ""):
        return None
    s = val.strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _make_action(
    ex_d: date,
    ev_type: str,
    sym: str,
    text: str,
    fetched_at: str,
    *,
    status: str = "unverified",
    ratio: float | None = None,
    ratio_old: float | None = None,
    ratio_new: float | None = None,
    amount: Decimal | None = None,
    issue_price: Decimal | None = None,
    record_date: date | None = None,
    reason: str = "",
) -> ActionRecord:
    act_id = f"nse_{sym.lower()}_{ex_d.isoformat()}_{ev_type.lower()}"
    return ActionRecord(
        ex_date=ex_d,
        type=ev_type,
        ratio=ratio,
        ratio_old=ratio_old,
        ratio_new=ratio_new,
        amount=amount,
        record_date=record_date,
        text=text,
        source="nse",
        source_ref=sym,
        fetched_at=fetched_at,
        symbol=sym.upper(),
        exchange="NSE",
        status=status,
        id=act_id,
        issue_price=issue_price,
        reason=reason,
    )


def _parse_split(subj: str, ex_d: date, sym: str, rec_d: date | None, now: str) -> ActionRecord:
    m = SPLIT_RE.search(subj)
    if m:
        f_val, t_val = float(m.group(1)), float(m.group(2))
        if f_val > 0.0 and t_val > 0.0:
            return _make_action(
                ex_d,
                "split",
                sym,
                subj,
                now,
                status="unverified",
                ratio=f_val / t_val,
                ratio_old=f_val,
                ratio_new=t_val,
                record_date=rec_d,
            )
    return _make_action(
        ex_d,
        "split",
        sym,
        subj,
        now,
        status="manual_required",
        record_date=rec_d,
        reason="Unparseable split face values",
    )


def _parse_bonus(subj: str, ex_d: date, sym: str, rec_d: date | None, now: str) -> ActionRecord:
    m = BONUS_RE.search(subj)
    if m:
        x, y = float(m.group(1)), float(m.group(2))
        if y > 0.0:
            return _make_action(
                ex_d,
                "bonus",
                sym,
                subj,
                now,
                status="unverified",
                ratio=x / y,
                ratio_new=x,
                ratio_old=y,
                record_date=rec_d,
            )
    return _make_action(
        ex_d,
        "bonus",
        sym,
        subj,
        now,
        status="manual_required",
        record_date=rec_d,
        reason="Missing bonus ratio",
    )


def _parse_rights(subj: str, ex_d: date, sym: str, rec_d: date | None, now: str) -> ActionRecord:
    m = RIGHTS_RE.search(subj)
    if not m:
        return _make_action(
            ex_d,
            "rights",
            sym,
            subj,
            now,
            status="manual_required",
            record_date=rec_d,
            reason="Unparseable rights ratio",
        )
    x, y = float(m.group(1)), float(m.group(2))
    r_val = (x / y) if y > 0.0 else None
    pm = RIGHTS_PRICE_RE.search(subj)
    if pm and "premium" not in subj.lower():
        try:
            price = Decimal(pm.group(1))
            return _make_action(
                ex_d,
                "rights",
                sym,
                subj,
                now,
                status="unverified",
                ratio=r_val,
                ratio_new=x,
                ratio_old=y,
                issue_price=price,
                record_date=rec_d,
            )
        except Exception:
            pass
    return _make_action(
        ex_d,
        "rights",
        sym,
        subj,
        now,
        status="manual_required",
        ratio=r_val,
        ratio_new=x,
        ratio_old=y,
        record_date=rec_d,
        reason="Rights issue price not explicitly stated",
    )


def _parse_dividend(subj: str, ex_d: date, sym: str, rec_d: date | None, now: str) -> ActionRecord:
    matches = DIV_AMT_RE.findall(subj)
    amts: list[Decimal] = []
    for g1, g2 in matches:
        raw_num = g1 or g2
        if raw_num:
            try:
                amts.append(Decimal(raw_num))
            except Exception:
                pass
    if amts:
        return _make_action(
            ex_d,
            "dividend",
            sym,
            subj,
            now,
            status="unverified",
            amount=sum(amts),
            record_date=rec_d,
        )
    return _make_action(
        ex_d,
        "dividend",
        sym,
        subj,
        now,
        status="manual_required",
        record_date=rec_d,
        reason="Dividend currency amount not stated",
    )


@lru_cache(maxsize=1)
def _load_purpose_rules(config_path: str | None = None) -> list[dict[str, Any]]:
    """Load non-price action purpose patterns from TOML."""
    paths_to_try = [
        Path(config_path) if config_path else None,
        Path("config/corporate_action_purposes.toml"),
        Path(__file__).resolve().parents[3] / "config" / "corporate_action_purposes.toml",
    ]
    for p in paths_to_try:
        if p and p.is_file():
            try:
                with open(p, "rb") as f:
                    data = tomllib.load(f)
                return list(data.get("purpose", []))
            except Exception:
                pass
    return []


def parse_nse_item(
    item: Mapping[str, Any],
    now_iso: str | None = None,
    purposes_config_path: str | None = None,
) -> Sequence[ActionRecord]:
    """Parse a single NSE corporate action JSON record into ActionRecords."""
    sym = str(item.get("symbol") or "").strip().upper()
    ex_d = _parse_nse_date(item.get("exDate"))
    if not sym or not ex_d:
        return []

    rec_d = _parse_nse_date(item.get("recDate"))
    subj = str(item.get("subject") or "").strip()
    s_lower = subj.lower()
    now = now_iso or datetime.now(UTC).isoformat()
    records: list[ActionRecord] = []

    # Check for Split
    if "split" in s_lower or "sub-division" in s_lower or "sub division" in s_lower:
        records.append(_parse_split(subj, ex_d, sym, rec_d, now))

    # Check for Bonus
    if "bonus" in s_lower:
        records.append(_parse_bonus(subj, ex_d, sym, rec_d, now))

    # Check for Rights
    if "right" in s_lower:
        records.append(_parse_rights(subj, ex_d, sym, rec_d, now))

    # Check for Dividend (ignore 'div' occurring inside 'sub-division' or 'sub division')
    s_no_subdiv = re.sub(r"sub[\s-]*division", "", s_lower)
    if "div" in s_no_subdiv or "dividend" in s_no_subdiv:
        records.append(_parse_dividend(subj, ex_d, sym, rec_d, now))

    if not records:
        # Check purpose rules from corporate_action_purposes.toml
        matched_rule = None
        for rule in _load_purpose_rules(purposes_config_path):
            pat = str(rule.get("pattern", "")).strip().lower()
            if pat and pat in s_lower:
                matched_rule = rule
                break

        if matched_rule is not None:
            r_class = str(matched_rule.get("class", "manual")).strip().lower()
            note = str(matched_rule.get("note", "")).strip()
            if r_class in ("ignore", "ignored"):
                records.append(
                    _make_action(
                        ex_d,
                        "ignored",
                        sym,
                        subj,
                        now,
                        status="ignored",
                        record_date=rec_d,
                        reason=note or "Ignored non-price action",
                    )
                )
            elif r_class == "split":
                records.append(_parse_split(subj, ex_d, sym, rec_d, now))
            elif r_class == "bonus":
                records.append(_parse_bonus(subj, ex_d, sym, rec_d, now))
            elif r_class == "rights":
                records.append(_parse_rights(subj, ex_d, sym, rec_d, now))
            elif r_class == "dividend":
                records.append(_parse_dividend(subj, ex_d, sym, rec_d, now))
            else:
                records.append(
                    _make_action(
                        ex_d,
                        "other",
                        sym,
                        subj,
                        now,
                        status="manual_required",
                        record_date=rec_d,
                        reason=note or "Unrecognised corporate action purpose",
                    )
                )
        else:
            records.append(
                _make_action(
                    ex_d,
                    "other",
                    sym,
                    subj,
                    now,
                    status="manual_required",
                    record_date=rec_d,
                    reason="Unrecognised corporate action purpose",
                )
            )

    return records
