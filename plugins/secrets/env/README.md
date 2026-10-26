# Environment Secrets Plugin

## Summary
The environment secrets plugin exposes credentials stored in operating system environment variables with prefix `ALGODESK_SECRET_<NAME>`. It provides read-only access to secrets, raising `NotSupported` when modification or deletion is attempted. Available secret names are discovered dynamically by scanning the environment for matching prefixes, with values never returned in the names listing.

## Capabilities & Usage
- Reads secrets prefixed with `ALGODESK_SECRET_` in uppercase.
- Read-only: `set()` and `delete()` explicitly raise `NotSupported`.
- Dynamic name discovery via `names()` without exposing secret values.
