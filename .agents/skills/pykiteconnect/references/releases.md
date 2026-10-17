# Releases

Version history for this repository (35 releases).

## v5.2.1: v5.2.1
**Published:** 2026-07-23

## What's Changed
* feat(order): add algo_id to order params by @rhnvrm in https://github.com/zerodha/pykiteconnect/pull/234


**Full Changelog**: https://github.com/zerodha/pykiteconnect/compare/v5.2.0...v5.2.1

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v5.2.1)

---

## v5.2.0: v5.2.0
**Published:** 2026-04-23

## What's Changed
* feat: add place_autoslice_order for orders exceeding freeze limits by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/227


**Full Changelog**: https://github.com/zerodha/pykiteconnect/compare/v5.1.0...v5.2.0

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v5.2.0)

---

## v5.1.0: v5.1.0
**Published:** 2026-03-27

## What's Changed
* feat: add market_protection param to place_order and modify_order by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/223


**Full Changelog**: https://github.com/zerodha/pykiteconnect/compare/v5.0.1...v5.1.0

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v5.1.0)

---

## v5.0.1: v5.0.1
**Published:** 2024-02-07

## What's Changed
* fix: downgrade urllib3 version to support openSSL in workflow by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/190


**Full Changelog**: https://github.com/zerodha/pykiteconnect/compare/v5.0.0...v5.0.1

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v5.0.1)

---

## v5.0.0: v5.0.0
**Published:** 2023-10-16

## Breaking change
* Dropped support for Python v2 completely. Supported versions are Python 3.5, 3.6, 3.7, 3.8, 3.9, 3.10.

## What's Changed
* feat: add virtual contract note API by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/183

**Full Changelog**: https://github.com/zerodha/pykiteconnect/compare/v4.2.0...v5.0.0

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v5.0.0)

---

## v5.0.0-beta: v5.0.0-beta
**Published:** 2023-07-31
**Pre-release**

## What's Changed
* feat: add virtual contract note API by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/183


**Full Changelog**: https://github.com/zerodha/pykiteconnect/compare/v4.2.0...v5.0.0-beta

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v5.0.0-beta)

---

## v4.2.0: v4.2.0
**Published:** 2023-01-09

## Features

- [Kite auctions API](https://kite.trade/docs/connect/v3/portfolio/#holdings-auction-list)
- [Added Charges in Margins calculations API](https://kite.trade/docs/connect/v3/margins/#margin-structure)

## What's Changed
* chores: use requests builtin JSON decoder by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/155
* feat: add py version 3.9 and 3.10 to tests by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/156
* feat: remove enum34 deps from install require by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/122
* fix: link correctly to kite.trade by @asdofindia in https://github.com/zerodha/pykiteconnect/pull/158
* feat: add deprecation warning for all BO properties by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/161
* fix: initialize ws object in KiteTicker class by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/157
* fix: gracefully exit websocket for exceeding max retry attempts by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/149
* chore: update kiteconnect login URL by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/164
* feat: create session object by default by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/167
* fix: KiteException: 'message' attribute error by @nasirxshah in https://github.com/zerodha/pykiteconnect/pull/166
* chore: remove depreciated bracket order params and tests by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/160
* feat: add get_auction_instruments, variety in place order and auction tests by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/169
* feat: add unit tests for order margins and charges by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/170
* Develop by @ranjanrak in https://github.com/zerodha/pykiteconnect/pull/171

## New Contributors
* @asdofindia made their first contribution in https://github.com/zerodha/pykiteconnect/pull/158
* @nasirxshah made their first contribution in https://github.com/zerodha/pykiteconnect/pull/166

**Full Changelog**: https://github.com/zerodha/pykiteconnect/compare/v4.1.0...v4.2.0

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v4.2.0)

---

## v4.1.0: v4.1.0
**Published:** 2022-05-24

Features added

- Iceberg orders
- TTL orders

Docs - https://kite.trade/docs/connect/v3/orders/

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v4.1.0)

---

## v4.0.2: v4.0.2
**Published:** 2022-03-17

**Full Changelog**: https://github.com/zerodha/pykiteconnect/compare/v4.0.0...v4.0.2

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v4.0.2)

---

## v4.0.0: Release v4.0.0
**Published:** 2021-11-23

## Breaking changes

* Renamed ticker fields as per [kite connect doc](https://kite.trade/docs/connect/v3/websocket/#quote-packet-structure)
* Renamed `bsecds` to `bcd` in `ticker.EXCHANGE_MAP`

The latest API doc for v4.x will be available [here](https://kite.trade/docs/pykiteconnect/v4). 

## What's Changed

* fix: update API usage order placement example 
* fix: update getting started example for order placement 
* chore: remove docs, fix copyright year + ztech 
* fix: update bsecds to bcd to be consistency with segment naming 
* feat: update bcd price divisor for ticker 
* fix: timestamp ticker response attributes 
* fix: ticker_attributes naming as per doc 

**Full Changelog**: https://github.com/zerodha/pykiteconnect/compare/v3.9.4...v4.0.0

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v4.0.0)

---

## v3.9.5: v3.9.5
**Published:** 2021-11-13

## Do not update to this version since it contains breaking changes. Instead use v4.x

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.9.5)

---

## v3.9.4: v3.9.4
**Published:** 2021-06-18

Changes - https://github.com/zerodha/pykiteconnect/compare/v3.9.2...v3.9.4

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.9.4)

---

## v3.9.2: Bugfixes
**Published:** 2021-01-28



[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.9.2)

---

## v3.9.1: Bugfixes 
**Published:** 2021-01-14

#105 

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.9.1)

---

## v3.9.0rc1: Release candidate - v3.9.0rc1
**Published:** 2020-12-03

- New order margins API
- Refactored base requests handler to accommodate JSON POST/PUT content types
- Refactored setup.py to remove `FakeDist` and other obscure ways of installing Twisted wheels since official wheels were not available
- Migrated Travis and Appveyor CI tests and builds to Github actions

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.9.0rc1)

---

## v3.9.0: v3.9.0
**Published:** 2020-12-03

- New order margins API
- Refactored base requests handler to accommodate JSON POST/PUT content types
- Refactored setup.py to remove FakeDist and other obscure ways of installing Twisted wheels since official wheels were not available
- Migrated Travis and Appveyor CI tests and builds to Github actions

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.9.0)

---

## v3.8.2: Bugfixes
**Published:** 2020-02-07

- Fix Pypi issue

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.8.2)

---

## v3.8.1: Bug fixes
**Published:** 2020-01-06

- Revert "fix: Do not subscribe if there was an unclean closure before"
- Typo in docs

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.8.1)

---

## v3.8.1-dev3: Test pypi release
**Published:** 2020-01-06
**Pre-release**



[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.8.1-dev3)

---

## v3.8.1-dev2: Test pypi release
**Published:** 2020-01-06
**Pre-release**



[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.8.1-dev2)

---

## v3.8.1-dev1: Test pypi release
**Published:** 2020-01-06
**Pre-release**



[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.8.1-dev1)

---

## v3.7.7: Bug fixes
**Published:** 2019-02-14

- Bug fix for abnormal websocket close

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.7.7)

---

## v3.7.3: Kite connect 3.7.3
**Published:** 2018-06-12

- Fix ticker datetime parsing issue on non unix OS

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.7.3)

---

## v3.7.2: Kite connect 3.7.2
**Published:** 2018-06-05

- Disable connection pooling by default (Fix: `request.ReadTimeout` issue)
- Fix: Reconnection should trigger resubscribe in spite of `on_open` cb

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.7.2)

---

## v3.7.0-beta11: Kite connect 3.0.0 beta 11
**Published:** 2018-05-09
**Pre-release**



[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.7.0-beta11)

---

## v3.7.0-beta8: Kite connect 3.0.0 beta 8
**Published:** 2018-02-27
**Pre-release**



[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.7.0-beta8)

---

## v3.7.0-beta4: Kite connect 3.0.0 beta 4 - Trigger range 
**Published:** 2018-02-05
**Pre-release**

## Changelog
- `trigger_range` method to fetch CO trigger range
- `quote`, `ltp`, `ohlc` and `trigger_range` takes instruments as args

## Trigger range example

    kite.trigger_range("BUY", "NSE:INFY", "NSE:RELIANCE")

## Quote calls example

    kite.ltp("NSE:INFY", "NSE:RELIANCE")
    kite.quote("NSE:INFY", "NSE:RELIANCE")
    kite.ohlc("NSE:INFY", "NSE:RELIANCE")

## [Kiteconnect v3 changelog](https://github.com/zerodhatech/pykiteconnect/releases/tag/v3.7.0-beta1)

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.7.0-beta4)

---

## v3.7.0-beta1: Kite connect 3.0.0 beta 1
**Published:** 2018-01-18
**Pre-release**

## Documentation
- [Python client documentation](https://kite.trade/docs/pykiteconnect/v3)
- [Kite Connect HTTP API documentation](https://kite.trade/docs/connect/v3)

## Install
    pip install kiteconnect --upgrade --pre

## New features

- method: `profile`
- method: `ohlc`
- method: `ltp`
- method: `renew_access_token`
- method: `invalidate_refresh_token`
- constants for products, order type, transaction type, variety, validity, exchanges and margin segments
- Param `disable_ssl` to `KiteConnect` initializer
- `quote` call supports multiple instruments call
- `exit_order` alias for `cancel_order`
- All datetime string fields has been converted to `datetime` object.
	- `orders`, `order_history`, `trades`, `order_trades`, `mf_orders` responses fields `order_timestamp`, `exchange_timestamp`, `fill_timestamp`
	- `mf_sips` fields `created`, `last_instalment`
	- `generate_session` field `login_time`
	- `quote` fields `timestamp`, `last_trade_time`
	- `instruments` field `expiry`
	- `mf_instruments` field `last_price_date`
- Requests thread pooling is enabled by default with defaults requests library settings [Read more](http://docs.python-requests.org/en/master/api/#requests.adapters.HTTPAdapter)

## API method name changes

| v2  					| v3 						|
| ----------------- 	| -------------------------	|
| request_access_token	| generate_session			|
| invalidate_token		| invalidate_access_token	|
| historical			| historical_data			|
| order_place 			| place_order				|
| order_modify 			| modify_order				|
| order_cancel 			| cancel_order				|
| product_modify 		| convert_position			|
| mf_order_place 		| place_mf_order			|
| mf_order_cancel 		| cancel_mf_order			|
| mf_sip_place 			| place_mf_sip				|
| mf_sip_modify 		| modify_mf_sip				|
| mf_sip_cancel 		| cancel_mf_sip			  	|
| set_session_hook  	| set_session_expiry_hook 	|
| orders(order_id)		| order_history(order_id) 	|
| trades(order_id)		| order_trades(order_id)  	|

## Param and other changes

- `historical_data` - Historical data accepts datetime object for `from_date` and `to_date` instead of string since historical api precision is upto minutes.
- `historical_data` - `date` field in output response is a datetime object instead of string.
- `modify_order`, `cancel_order` and `exit_order` takes variety as first param instead of `order_id`
- [Changes in `generate_session` response structure](https://kite.trade/docs/connect/v3/user/#response-attributes)
- [Changes in `positions` response structure](https://kite.trade/docs/connect/v3/portfolio/#response-attributes_1)
- [Changes in `quote` response structure](https://kite.trade/docs/connect/v3/market-quotes/#retrieving-full-market-quotes)
- [Changes in `place_order` params](https://kite.trade/docs/connect/v3/orders/#bracket-order-bo-parameters)

## Deprecated from v2

- `exceptions.UserException`
- `exceptions.ClientNetworkException`
- `exceptions.TwoFAException`
- Param `micro_cache` from `KiteConnect` initializer
- Param `order_id` from `orders` call (Renamed to `order_history`)
- Param `order_id` from `trades` call (Renamed to `order_trades`)

## KiteTicker changes

- Rename class `WebSocket` to `KiteTicker`
- `KiteTicker` initializer param `public_token` is replaced with `access_token`
- Added `KiteTicker` param `reconnect` to enable/disable auto re-connection.
- Auto re-connection is enabled by default (`reconnect` is `True` by default)
- `reconnect_interval` is deprecated and replaced with `reconnect_max_delay`
- Rename: `reconnect_tries` to `reconnect_max_tries`
- Auto reconnect uses exponential back-off algorithm instead of fixed reconnect interval (https://en.wikipedia.org/wiki/Exponential_backoff)
- Underlying WebSocket library is replaced with Autohbahn Python client (Supports 2,7+, 3.3+) for more stability.
- Added param `connect_timeout` to `KiteTicker` initializer
- Added method `stop_retry` to stop auto reconnect while auto re-connection in progress.

## KiteTicker callback changes

- `on_ticks(ws, ticks)` -  Triggered when ticks are received.
	- `ticks` - List of `tick` object. Check below for sample structure.
- `on_close(ws, code, reason)` -  Triggered when connection is closed.
	- `code` - WebSocket standard close event code (https://developer.mozilla.org/en-US/docs/Web/API/CloseEvent)
	- `reason` - DOMString indicating the reason the server closed the connection
- `on_error(ws, code, reason)` -  Triggered when connection is closed with an error.
	- `code` - WebSocket standard close event code (https://developer.mozilla.org/en-US/docs/Web/API/CloseEvent)
	- `reason` - DOMString indicating the reason the server closed the connection
- `on_connect` -  Triggered when connection is established successfully.
	- `response` - Response received from server on successful connection.
- `on_message(ws, payload, is_binary)` -  Triggered when message is received from the server.
	- `payload` - Raw response from the server (either text or binary).
	- `is_binary` - Bool to check if response is binary type.
- `on_reconnect(ws, attempts_count)` -  Triggered when auto re-connection is attempted.
	- `attempts_count` - Current reconnect attempt number.
- `on_noreconnect(ws)` -  Triggered when number of auto re-connection attempts exceeds `reconnect_tries`.


## KiteTicker deprecated methods

- `enable_reconnect`
- `disable_reconnect`
- `reconnect` - `reconnect` can be set while initializing `KiteTicker`

## KiteTicker response changes

- Full mode has following new fields
    - `last_trade_time` - Last trade time (Python datetime object or None)
    - `oi` - Open interest
    - `oi_high` - Day's open interest high
    - `oi_low` - Day's open interest low
    - `timestamp` - Tick timestamp (Python datetime object or None)

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.7.0-beta1)

---

## v3.7.0: Kite connect 3.7.0
**Published:** 2018-05-20

- [Python client documentation](https://kite.trade/docs/pykiteconnect/v3)
- [Kite Connect HTTP API documentation](https://kite.trade/docs/connect/v3)

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.7.0)

---

## v3.6.2: OHLC and LTP 
**Published:** 2017-12-12

`ohlc` - Retrieve OHLC and market depth for list of instruments
`ltp` - Retrieve last price for list of instruments

Both methods accepts params: `instruments` which is a list of instruments. Instrument are in the format of `tradingsymbol:exchange`. For example NSE:INFY

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.6.2)

---

## v3.6.1: Continuous chart and other bug fixes
**Published:** 2017-09-14

- Now you can fetch continuous chart for futures and options contract in historical api call
- Bug fix with order modify

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.6.1)

---

## v3.6.0: Mutual fund API
**Published:** 2017-08-16

Mutual funds API calls has been implemented for
- Place/Cancel and get mutual fund orders
- Place/Modify/Cancel and get SIP's
- Get mutual fund holdings
- Get mutual fund instruments

Please check documentation for [list of calls available](https://kite.trade/docs/pykiteconnect/#kiteconnect.KiteConnect.mf_holdings).

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.6.0)

---

## 3.5: Auto reconnect and bug fixes 
**Published:** 2017-07-17

## Added features
Auto reconnect WebSocket connection in case of network failure. Refer README for usage details -
 https://github.com/rainmattertech/pykiteconnect#websocket-usage

## Bug fixes
1. Fixed `mode` was sent incorrectly as `quote` while subscribed for `full` mode (3d78dfcc74b3af15af1726ad925d619a3e16c770)
2. Send empty buy and sell lists instead of empty depth if depths are not available. (1c2e0cdcec0e370c1070cf9877f5f2c7cd4b0cfa)

[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/3.5)

---

## v3.4.1: Bug fixes
**Published:** 2017-01-21



[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.4.1)

---

## v3.3: v3.3
**Published:** 2016-10-25

Released version 3.3 with following fixes and features 
- Added tag support to order APIs 
- Added proxy support for api and websocket streaming 
- Fixed market depth orders integer overflow issue.


[View on GitHub](https://github.com/zerodha/pykiteconnect/releases/tag/v3.3)

---

