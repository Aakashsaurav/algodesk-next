# Live Sample Feed Fixtures

## Summary

This directory contains real market data events recorded from the live Upstox Market Data Feed V3 stream for golden decoding, sorting, and invariant testing.

## Details

- **Origin**: Upstox Market Data Feed V3 captured using `algodesk feed probe --record`.
- **Date**: 2026-10-23.
- **Content**: Market data only. Contains strictly public tick, quote, and feed connection status messages. No account numbers, credentials, tokens, or PII are present.
- **Size**: 19 event frames, ~8.5 KB total (well within the 200 KB ceiling).
