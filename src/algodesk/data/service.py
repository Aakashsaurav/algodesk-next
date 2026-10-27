"""Data service implementing the FR-DAT-1 interface.

Pass-through layer routing data requests to configured data sources.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from algodesk.api.market import Bar, Instrument, SeriesKey
from algodesk.api.protocols import DataSource


class DataService:
    """Unified market data access layer routing to configured DataSources."""

    __slots__ = ("_default_source", "_sources")

    def __init__(
        self,
        sources: Mapping[str, DataSource],
        default_source: str = "yfinance",
    ) -> None:
        self._sources = dict(sources)
        self._default_source = default_source

    @property
    def default_source(self) -> str:
        return self._default_source

    def _resolve_source(self, name: str | None) -> DataSource:
        src_name = name or self._default_source
        if src_name not in self._sources:
            raise KeyError(
                f"Data source '{src_name}' not found. Configured: {list(self._sources.keys())}"
            )
        return self._sources[src_name]

    def history(
        self,
        key: SeriesKey,
        start: datetime,
        end: datetime,
        source: str | None = None,
    ) -> Sequence[Bar]:
        """Fetch historical bars for key between start and end."""
        ds = self._resolve_source(source)
        return list(ds.history(key, start, end))

    def bars(
        self,
        key: SeriesKey,
        start: datetime,
        end: datetime,
        source: str | None = None,
    ) -> Sequence[Bar]:
        """Alias for history (FR-DAT-1)."""
        return self.history(key, start, end, source=source)

    def instruments(self, source: str | None = None) -> Sequence[Instrument]:
        """Fetch tradable instruments from source."""
        ds = self._resolve_source(source)
        return list(ds.instruments())
