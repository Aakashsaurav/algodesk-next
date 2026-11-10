"""Vendor market data importer broker plugin."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from algodesk.api import (
    Bar,
    DataSource,
    Instrument,
    InstrumentId,
    MarketEvent,
    NotSupported,
    PluginBase,
    PluginContext,
    PluginManifest,
    SeriesKey,
)

from .config_schema import ImporterConfig


class ImporterBrokerPlugin(PluginBase, DataSource):
    """DataSource plugin importing vendor CSV and Parquet market data files."""

    def __init__(
        self,
        manifest: PluginManifest,
        config: ImporterConfig | dict[str, Any] | None = None,
        ctx: PluginContext | None = None,
    ) -> None:
        if isinstance(config, ImporterConfig):
            self._cfg = config
            raw_config: Mapping[str, object] = config.model_dump()
        elif isinstance(config, Mapping):
            self._cfg = ImporterConfig.model_validate(config)
            raw_config = config
        else:
            self._cfg = ImporterConfig()
            raw_config = self._cfg.model_dump()

        super().__init__(manifest, raw_config, cast(PluginContext, ctx))

    def instruments(self) -> Iterable[Instrument]:
        """Listing instrument master is not supported directly by vendor file importer."""
        raise NotSupported(
            what="Vendor file importer does not maintain a live instrument catalog",
            where="ImporterBrokerPlugin.instruments",
            fix="Import structured instruments through InstrumentStore or daily master snapshot",
        )

    def history(self, key: SeriesKey, start: datetime, end: datetime) -> Iterable[Bar]:
        """Fetch historical bars from imported data."""
        self.require("history")
        # History is serviced directly through BarStore once imported
        return []

    def stream(self, instruments: Sequence[InstrumentId]) -> AsyncIterator[MarketEvent]:
        """Streaming real-time market data is not supported by file importer."""
        raise NotSupported(
            what="Vendor file importer does not support real-time streaming",
            where="ImporterBrokerPlugin.stream",
            fix="Use live suitable broker plugin (e.g. upstox) for streaming market events",
        )
