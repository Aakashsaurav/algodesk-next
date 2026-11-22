"""Fee call mapping resolving instrument and product to fee schedule attributes."""

from __future__ import annotations

from typing import Any

from algodesk.api.market import Instrument, parse_id


def resolve_fee_segment_and_product(
    instrument: Any,
    product: Any = None,
    default_exchange: str = "nse",
) -> tuple[str, str, str, str | None]:
    """Resolve (segment, product, exchange, instrument_type) for fee calculations.

    Canonical mapping used identically across paper, replay, equivalence, calibrate,
    and tick-fidelity execution modes.

    Returns:
        (segment, product, exchange, instrument_type)
        where:
        - segment: "eq" or "fno" (or "com")
        - product: "CNC", "MIS", or "NRML"
        - exchange: "nse", "bse", "mcx", etc.
        - instrument_type: None, "fut", or "opt"
    """
    exch = default_exchange
    seg = "EQ"
    sym = ""
    opt_type: str | None = None

    if isinstance(instrument, Instrument) or (
        hasattr(instrument, "exchange") and hasattr(instrument, "segment")
    ):
        exch = str(instrument.exchange).lower()
        seg = str(instrument.segment).upper()
        sym = str(getattr(instrument, "symbol", "")).upper()
        opt_type = getattr(instrument, "option_type", None)
    elif isinstance(instrument, str) and instrument:
        try:
            parts = parse_id(instrument)
            exch = parts.exchange.lower()
            seg = parts.segment.upper()
            sym = parts.symbol.upper()
            opt_type = parts.option_type
        except Exception:
            components = instrument.split(":")
            if len(components) >= 1 and components[0]:
                exch = components[0].lower()
            if len(components) >= 2 and components[1]:
                seg = components[1].upper()
            if len(components) >= 3 and components[2]:
                sym = components[2].upper()
            if len(components) >= 6 and components[5] in ("CE", "PE"):
                opt_type = components[5]
    elif hasattr(instrument, "instrument"):
        return resolve_fee_segment_and_product(
            instrument.instrument,
            getattr(instrument, "product", product),
            default_exchange=default_exchange,
        )

    # Resolve product string
    if hasattr(product, "value"):
        prod_str = str(product.value).upper()
    elif product is not None and str(product).strip():
        prod_str = str(product).strip().upper()
    else:
        prod_str = "CNC"

    if prod_str not in ("CNC", "MIS", "NRML"):
        prod_str = "CNC"

    fee_exchange = exch.lower()

    # Resolve segment and instrument_type
    itype: str | None = None
    if exch.lower() == "mcx" or seg in ("COM", "COMMODITY"):
        fee_segment = "com"
        itype = "fut"
    elif (
        seg == "OPT"
        or opt_type in ("CE", "PE")
        or (
            seg == "FNO"
            and (sym.endswith("CE") or sym.endswith("PE") or " CE" in sym or " PE" in sym)
        )
    ):
        fee_segment = "fno"
        itype = "opt"
    elif seg in ("FUT", "FNO") or "FUT" in sym or sym.endswith("FUT"):
        fee_segment = "fno"
        itype = "fut"
    else:
        fee_segment = "eq"
        itype = None

    return fee_segment, prod_str, fee_exchange, itype
