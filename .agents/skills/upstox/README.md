# Upstox Agent Skill


A native agent skill for interacting with NSE/BSE equities, F&O, and MCX commodities via the [Upstox Developer API](https://upstox.com/developer/api-documentation/).


This integration allows AI agents to execute trades, stream market data, and manage portfolios using the [Agent Skills](https://agentskills.io) open standard.

### Features

- **Order Execution** – Place, modify, and manage live orders across segments.
- **Market Feeds** – Stream low-latency market data and live order books.
- **Portfolio Access** – Read account balances, current holdings, and open positions.

---

### Compatibility

Built to comply with the `SKILL.md` specification, providing compatibility with:

- **Claude Code**
- **Codex**
- Any agent framework supporting the SKILL standard.

---

## Installation

### Global install
```bash
npm install -g skills
skills add upstox/upstox-skills --skill upstox
```

### Claude Code or Codex
```bash
npx skills add upstox/upstox-skills --skill upstox
```

> Replace `upstox/upstox-skills` with your fork's `owner/repo` until this is published.

---

## Requirements

- Python 3.8+
- Install dependencies:
  ```bash
  pip install upstox-python-sdk
  ```
- An Upstox account with an **access token** ([developer portal](https://account.upstox.com/developer/apps))

### Authentication

The skill only needs an **access token** to run. Provide it one of two ways:

**1. Environment variable (recommended)**
```bash
export UPSTOX_ACCESS_TOKEN="your-daily-token"
```

**2. Config file** — copy `skills/upstox/config.json.example` to `skills/upstox/config.json` and fill in `access_token`:
```json
{
  "access_token": "your-daily-token"
}
```
The environment variable takes precedence if both are set. `config.json` is git-ignored, so your token is never committed.

> **Daily token:** Upstox access tokens expire at the end of each trading day — refresh it daily. Generate one from the [Upstox developer portal](https://account.upstox.com/developer/apps).

---

## What's Included

```
skills/upstox/
├── SKILL.md                          # Entry point — setup, v2/v3 guide, safety, core patterns
│
├── references/                       # Deep-dive docs loaded on demand
│   ├── orders.md                     # Place/modify/cancel (v3), multi-order, exit
│   ├── gtt-orders.md                 # Good-till-triggered single & multi-leg (OrderApiV3)
│   ├── portfolio.md                  # Holdings, positions, conversion, realised P&L
│   ├── market-data.md                # LTP/OHLC/quotes (v3), historical candles, status
│   ├── option-chain.md               # Put-call chain, Greeks, PCR, max pain
│   ├── margins.md                    # Funds, required margin, brokerage charges
│   ├── instruments.md                # Instrument master, symbol resolution, lot sizes
│   ├── kill-switch.md                # Halt trading in a segment (risk control)
│   ├── websocket.md                  # MarketDataStreamerV3 + PortfolioDataStreamer
│   └── errors.md                     # Error codes, messages, retry patterns
│
├── scripts/
│   ├── upstox_helpers.py             # get_client() factory (env / sandbox) + helpers
│   ├── instrument_search.py          # Name → instrument_key via the Search API
│   └── validate_order.py             # Pre-flight order validation with guardrails
│
└── examples/
    ├── place_equity_order.py          # Equity order with validation + confirmation
    ├── place_gtt_order.py             # Single-leg GTT trigger order
    ├── portfolio_summary.py           # Holdings + positions + funds dashboard
    ├── historical_candles.py          # v3 historical candles + quick stats
    ├── option_chain_analysis.py       # ATM options, PCR, max pain for Nifty
    ├── market_quote.py                # Full market quotes (OHLC + depth + OI)
    ├── instrument_search.py           # Search API: free-text + ATM-relative options
    ├── bull_call_spread.py            # Bullish option strategy (2 legs)
    ├── bear_butterfly.py              # Bearish put-butterfly strategy (3 legs)
    └── short_strangle.py              # Neutral short-strangle strategy (2 legs)
```

> **Accuracy note:** every SDK class, method, request model, field, enum, and
> instrument-file URL in this skill is verified against the
> [official upstox-python SDK source](https://github.com/upstox/upstox-python).
> The most important rule is the **v2 vs v3 split** — see [`SKILL.md`](skills/upstox/SKILL.md).

---

## Built-In Safety Guardrails

| Safeguard | How it protects you |
|------|-------------|
| **Test before going live** | Encourages rehearsing order flows in the Upstox **sandbox** (`Configuration(sandbox=True)`) — place and verify test orders with no real money before any live trade |
| **Always asks first** | Shows you a complete order summary and waits for your explicit `yes` before anything is placed |
| **Plays it safe with pricing** | Places limit orders by default — it won't fire off a market order unless you specifically ask for one |
| **Starts small** | Defaults to just 1 share (or 1 lot) when you don't say how many, so nothing is over-ordered by mistake |
| **Catches bad F&O quantities** | Blocks futures & options orders unless the quantity is a valid multiple of the contract's lot size |
| **Shields market orders** | Even if you ask for a market order, Upstox's Market Price Protection keeps the fill within a safe price band — and you can tighten that band yourself with `market_protection` |
| **Lets you pull the plug** | Built-in kill-switch support so you can instantly halt all trading in a segment (cancels pending orders and blocks new ones) when you need to step away |
| **Keeps your credentials safe** | Never hardcodes your access token — it's read from an environment variable or a git-ignored `config.json` |

---

## Example Prompts

**Orders**
- "Buy 10 SBIN at 820"
- "Sell ITC at market, intraday"
- "Place a GTT to buy Wipro when it falls to 440"
- "Cancel order 240XXXXXX123"
- "Square off all my F&O positions"

**Portfolio**
- "What am I holding right now?"
- "Show my open positions"
- "How much cash do I have free?"
- "Move my ITC intraday position to delivery"

**Market Data**
- "Last price of Bank Nifty"
- "Daily candles for SBIN this month"
- "Show the order book depth for Tata Motors"

**Options**
- "Pull the Nifty option chain for this week's expiry"
- "Put-call ratio for Bank Nifty"
- "Which Nifty strike is closest to spot?"
- "Where's max pain on Nifty?"

**Option strategies**
- "Build a bull call spread on Nifty"
- "Put on a short strangle on Bank Nifty"
- "Set up a bear put butterfly on Nifty"

---

## SDK Reference

- **PyPI**: `upstox-python-sdk`
- **Base URL**: `https://api.upstox.com/`
- **API Docs**: https://upstox.com/developer/api-documentation/
- **Sandbox**: https://upstox.com/developer/api-documentation/sandbox
- **Community**: https://community.upstox.com/c/developer-api

---

## License

MIT
