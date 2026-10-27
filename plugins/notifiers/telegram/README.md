# Telegram Notifier Plugin

## Summary
The `notifiers/telegram` plugin delivers outbound operational alerts, trade execution updates, heartbeat pulses, and end-of-day summaries from AlgoDesk Next to a Telegram chat using the Telegram Bot API. It implements a non-blocking architecture using a bounded in-memory queue, background worker thread, rate limiter, exponential back-off with jitter, and circuit breaker. The `send()` method never raises or blocks the trading engine.

## Configuration & Secrets
Secrets are referenced by standard reference identifiers and never stored in plain text:
- `bot_token`: `secret:telegram_bot_token`
- `chat_id`: `secret:telegram_chat_id`

Runtime behavior (queue capacity, dispatch rate limit, retry count, back-off parameters, and circuit breaker limits) is centrally managed in `config/params/runtime.toml`.
