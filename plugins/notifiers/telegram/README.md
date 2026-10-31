# Telegram Notifier Plugin

## Summary
The `notifiers/telegram` plugin delivers outbound operational alerts, trade execution updates, heartbeat pulses, and end-of-day summaries from AlgoDesk Next to a Telegram chat using the Telegram Bot API. It implements a non-blocking architecture using a bounded in-memory queue, background worker thread, rate limiter, exponential back-off with jitter, and circuit breaker. The `send()` method never raises or blocks the trading engine.

## Configuration & Secrets
Secrets are referenced by standard reference identifiers and never stored in plain text:
- `bot_token`: `secret:telegram_bot_token`
- `chat_id`: `secret:telegram_chat_id`

Runtime behavior (queue capacity, dispatch rate limit, retry count, back-off parameters, and circuit breaker limits) is centrally managed in `config/params/runtime.toml`.

## Changes after M1-6 (2026-10-30)
- **client.py**: Refactored retry mechanism to abort immediately on permanent HTTP client errors (400, 401, 403, 404) rather than retrying futile requests.
- **plugin.py**: Guarded client initialization and added whitespace/empty token validation to cleanly disable notifier dispatch when secrets are absent.
