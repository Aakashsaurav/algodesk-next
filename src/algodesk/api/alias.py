"""Instrument alias resolution and legacy identifier mapping.

Supports transparent resolution of legacy identifiers (such as
'MCX:FNO:GOLD FUT 04 DEC 26') to canonical InstrumentId
('MCX:FUT:GOLD:2026-12-04').
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from algodesk.api.errors import DataError
from algodesk.api.market import InstrumentId, parse_id

_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

_FUT_RE = re.compile(r"^([A-Z0-9]+)\s+FUT\s+(\d{1,2})\s+([A-Z]{3})\s+(\d{2})$")
_OPT_RE = re.compile(r"^([A-Z0-9]+)\s+([0-9.]+)\s+(CE|PE)\s+(\d{1,2})\s+([A-Z]{3})\s+(\d{2})$")

# Global in-memory alias registry
_KNOWN_ALIASES: dict[str, InstrumentId] = {
    "MCX:FNO:GOLD FUT 04 DEC 26": InstrumentId("MCX:FUT:GOLD:2026-12-04"),
}


def register_alias(legacy_id: str, canonical_id: str | InstrumentId) -> None:
    """Register an explicit alias mapping."""
    _KNOWN_ALIASES[legacy_id.strip()] = InstrumentId(str(canonical_id).strip())


def parse_legacy_symbol(
    sym: str,
) -> tuple[str, str, date, Decimal | None, str | None] | None:
    """Parse legacy broker trading symbol string.

    Returns:
        (segment, underlying, expiry, strike, option_type) or None.
    """
    cleaned = sym.strip().upper()
    m_fut = _FUT_RE.match(cleaned)
    if m_fut:
        und, day_s, mon_s, yr_s = m_fut.groups()
        if mon_s not in _MONTHS:
            return None
        exp = date(2000 + int(yr_s), _MONTHS[mon_s], int(day_s))
        return ("FUT", und, exp, None, None)

    m_opt = _OPT_RE.match(cleaned)
    if m_opt:
        und, strike_s, opt_type, day_s, mon_s, yr_s = m_opt.groups()
        if mon_s not in _MONTHS:
            return None
        exp = date(2000 + int(yr_s), _MONTHS[mon_s], int(day_s))
        strike = Decimal(strike_s)
        return ("OPT", und, exp, strike, opt_type)

    return None


def resolve_alias(identifier: str | InstrumentId, manifest: Any | None = None) -> InstrumentId:
    """Resolve an instrument identifier (legacy or canonical) to a canonical InstrumentId.

    Fails closed if resolution is ambiguous.
    """
    if not isinstance(identifier, str):
        raise TypeError(f"Instrument identifier must be a string, got {type(identifier).__name__}")
    raw = identifier.strip()
    if not raw:
        raise ValueError("Instrument identifier cannot be empty")

    # 1. Check in-memory registered aliases
    if raw in _KNOWN_ALIASES:
        return _KNOWN_ALIASES[raw]

    # 2. Check manifest if provided
    if manifest is not None and hasattr(manifest, "resolve_alias"):
        try:
            m_res = manifest.resolve_alias(raw)
            if m_res:
                return InstrumentId(m_res)
        except Exception:
            pass

    # 3. Check if already canonical without spaces
    if " " not in raw:
        try:
            parsed = parse_id(raw)
            if parsed.segment in ("EQ", "FUT", "OPT", "INDEX", "COM"):
                return InstrumentId(raw)
        except Exception:
            pass

    # 4. Check for legacy format: EXCHANGE:FNO:TRADING_SYMBOL or EXCHANGE:SEGMENT:SYMBOL with spaces
    candidates: list[InstrumentId] = []
    if ":" in raw:
        parts = raw.split(":", 2)
        if len(parts) >= 3:
            exchange = parts[0].upper()
            _seg = parts[1].upper()
            sym_part = parts[2]
            parsed_sym = parse_legacy_symbol(sym_part)
            if parsed_sym is not None:
                new_seg, und, exp, strike, opt_type = parsed_sym
                if strike is not None and opt_type is not None:
                    if strike == strike.to_integral_value():
                        k_str = str(int(strike))
                    else:
                        k_str = format(strike.normalize(), "f")
                    cand = InstrumentId(
                        f"{exchange}:{new_seg}:{und}:{exp.isoformat()}:{k_str}:{opt_type}"
                    )
                else:
                    cand = InstrumentId(f"{exchange}:{new_seg}:{und}:{exp.isoformat()}")
                candidates.append(cand)

    # If parsing produced multiple distinct interpretations, fail closed
    unique_candidates = sorted(list(set(candidates)))
    if len(unique_candidates) > 1:
        raise DataError(
            what=(
                f"Ambiguous legacy instrument ID '{identifier}'. "
                f"Candidates: {', '.join(unique_candidates)}"
            ),
            where="alias.resolve_alias",
            fix="Specify canonical InstrumentId explicitly",
        )
    if len(unique_candidates) == 1:
        return unique_candidates[0]

    # Fallback to parse_id validation
    return InstrumentId(raw)
