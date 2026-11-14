# sma_trend_py Screen Plugin

## Summary
The `sma_trend_py` plugin provides a reference Python implementation of the SMA(50) > SMA(200) trend test screen for AlgoDesk Next. It evaluates daily completed bars across an instrument universe without look-ahead bias, outputting conforming screen rows with identical logic and result hashes to `config/screens/sma_trend_test.toml`.

## Specifications
- **Timeframe**: Daily (`1d`).
- **Adjustment**: `total`.
- **Logic**: Symbol passes if simple moving average with period 50 exceeds simple moving average with period 200 on the last completed session.
- **Outputs**: `sma_50`, `sma_200`.

