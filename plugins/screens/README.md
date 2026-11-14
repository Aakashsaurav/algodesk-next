# Screen Plugins

## Summary
The `plugins/screens` directory hosts Python-based screen plugins for AlgoDesk Next. Screen plugins filter and rank an instrument universe across historical market data using standard technical indicators and completed bar series. They implement the `Screen` protocol in `algodesk.api.screens`, evaluating read-only `FeatureView` instances with zero look-ahead bias and no direct dependencies on data services, brokers, or venues.

## Protocol and Conventions
Every screen plugin implements the `Screen` protocol:
- **`spec: ScreenSpec`**: Declares name, description, timeframe (e.g. `1d`), adjustment mode (`total`, `splits`, `none`), and required indicator dependencies.
- **`evaluate(features: FeatureView) -> Sequence[ScreenRow]`**: Evaluates each non-skipped symbol in the universe, returning deterministic `ScreenRow` objects with pass status, score, and feature values.

Screen plugins can be scaffolded using `algodesk new plugin screen <name>`.

