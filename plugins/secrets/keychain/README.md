# macOS Keychain Secrets Plugin

## Summary
The macOS Keychain secrets plugin provides persistent, encrypted secret storage using the native macOS `security` CLI tool without shell invocation. Secrets are addressed by name under a configurable service identifier, with secret names indexed in `.algodesk/secrets_index.json` for name discovery. Secret values are never logged, echoed, or included in index files. Non-macOS systems raise a descriptive configuration error guiding the operator to alternate secret providers.

## Capabilities & Usage
- Reads, writes, and deletes secrets in the macOS Keychain.
- Zero credential leakage: values are retrieved in-memory only and never logged.
- Indexing tracks registered names under `.algodesk/secrets_index.json`.
