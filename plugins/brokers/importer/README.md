# Importer Broker Plugin

## Summary
The `importer` broker plugin enables ingestion of vendor CSV and Parquet market data files into AlgoDesk Next. Using structured column mapping definitions, it resolves timestamps with explicit timezone conversions, translates symbols to canonical `InstrumentId` format through the instrument master, routes records through the core `BarValidator` and `BarStore`, and records file SHA-256 digests for end-to-end provenance.

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `default_exchange` | string | `NSE` | Default exchange for unqualified bare symbols |
| `mapping_file` | string | `None` | Path to default column mapping TOML/JSON file |

## Column Mapping Schema

Mapping files define column bindings for vendor bars or ticks:

```toml
kind = "bars"            # "bars" or "ticks"
timeframe = "1m"         # "1m" or "1d" (for bars)
timezone = "Asia/Kolkata" # Source timestamp timezone
symbol = "NSE:EQ:RELIANCE" # Default symbol if omitted per row

[columns]
timestamp = "Date"
open = "Open"
high = "High"
low = "Low"
close = "Close"
volume = "Volume"
```

## CLI Usage

```bash
algodesk data import data/vendor/reliance_1m.csv --mapping config/mappings/generic_bars.toml
algodesk data import data/vendor/reliance_1m.parquet --mapping config/mappings/generic_bars.toml --dry-run
```

