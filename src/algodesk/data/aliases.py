"""Instrument alias resolution and legacy identifier mapping (compat module).

See algodesk.api.alias for implementation.
"""

from algodesk.api.alias import (
    _FUT_RE,
    _KNOWN_ALIASES,
    _MONTHS,
    _OPT_RE,
    parse_legacy_symbol,
    register_alias,
    resolve_alias,
)

__all__ = [
    "resolve_alias",
    "register_alias",
    "parse_legacy_symbol",
    "_MONTHS",
    "_FUT_RE",
    "_OPT_RE",
    "_KNOWN_ALIASES",
]
