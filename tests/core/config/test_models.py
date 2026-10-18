"""Tests for algodesk.config.models — pydantic models and validation rules."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from algodesk.api.kinds import Mode


def _make_valid_run_dict() -> dict[str, object]:
    return {
        "allocated_capital": Decimal("100000"),
        "run": {"name": "demo_paper", "mode": Mode.PAPER},
        "data_source": {"plugin": "upstox"},
        "execution_venue": {"plugin": "sim", "fill_model": "conservative_book"},
        "calendar": {"plugin": "nse"},
        "secrets": {"plugin": "keychain"},
        "fees": {"schedule": "zerodha_nse"},
        "deployment": {"profile": "local"},
        "universe": {"screen": "nifty_liquid_100"},
        "strategies": [
            {
                "plugin": "sma_cross",
                "risk_profile": "conservative",
                "params": {"fast": 20, "slow": 50},
            }
        ],
        "notifiers": [{"plugin": "telegram", "heartbeat_minutes": 15}],
    }


def test_platform_config_defaults() -> None:
    """PlatformConfig initializes with default paths, logging, and plugin_dirs."""
    from algodesk.config.models import PlatformConfig

    cfg = PlatformConfig()
    assert cfg.project == "algodesk"
    assert cfg.paths.data_dir == "data"
    assert cfg.paths.logs_dir == "logs"
    assert cfg.paths.results_dir == "results"
    assert cfg.paths.local_dir == ".local"
    assert cfg.logging.level == "INFO"
    assert cfg.logging.format == "json"
    assert cfg.plugin_dirs == ["plugins"]


def test_models_are_frozen() -> None:
    """All configuration models are frozen against modification."""
    from algodesk.config.models import (
        LoggingConfig,
        PathsConfig,
        PlatformConfig,
        PluginRef,
        RunConfig,
        RunMeta,
    )

    plat = PlatformConfig()
    with pytest.raises(ValidationError):
        plat.project = "other"  # type: ignore[misc]

    paths = PathsConfig()
    with pytest.raises(ValidationError):
        paths.data_dir = "other"  # type: ignore[misc]

    logging = LoggingConfig()
    with pytest.raises(ValidationError):
        logging.level = "DEBUG"  # type: ignore[misc]

    pref = PluginRef(plugin="zerodha", key="val")
    with pytest.raises(ValidationError):
        pref.plugin = "upstox"  # type: ignore[misc]

    run_meta = RunMeta(name="test", mode=Mode.BACKTEST)
    with pytest.raises(ValidationError):
        run_meta.name = "other"  # type: ignore[misc]

    run = RunConfig.model_validate(_make_valid_run_dict())
    with pytest.raises(ValidationError):
        run.allocated_capital = Decimal("200000")  # type: ignore[misc]


def test_plugin_ref_keeps_extra_options() -> None:
    """PluginRef stores arbitrary extra options and allows attribute access."""
    from algodesk.config.models import PluginRef

    ref = PluginRef(plugin="sim", fill_model="conservative_book", latency_ms=10)
    assert ref.plugin == "sim"
    assert ref.model_extra is not None
    assert ref.model_extra == {"fill_model": "conservative_book", "latency_ms": 10}
    assert ref.model_extra["fill_model"] == "conservative_book"
    assert ref.model_extra["latency_ms"] == 10


def test_strategy_config_keeps_extra_options() -> None:
    """StrategyConfig extends PluginRef and retains arbitrary extra options."""
    from algodesk.config.models import StrategyConfig

    strat = StrategyConfig(
        plugin="ema_trend",
        risk_profile="aggressive",
        params={"window": 14},
        custom_tag="alpha",
    )
    assert strat.plugin == "ema_trend"
    assert strat.risk_profile == "aggressive"
    assert strat.params == {"window": 14}
    assert strat.model_extra is not None
    assert strat.model_extra["custom_tag"] == "alpha"


def test_paths_relative_validation() -> None:
    """PathsConfig rejects absolute paths and paths with traversal ('..')."""
    from algodesk.config.models import PathsConfig

    with pytest.raises(ValidationError, match="relative path without traversal"):
        PathsConfig(data_dir="/etc/passwd")

    with pytest.raises(ValidationError, match="relative path without traversal"):
        PathsConfig(logs_dir="../logs")


def test_logging_level_validation() -> None:
    """LoggingConfig validates level against allowed set and normalizes case."""
    from algodesk.config.models import LoggingConfig

    cfg = LoggingConfig(level="debug")
    assert cfg.level == "DEBUG"

    with pytest.raises(ValidationError, match="Invalid log level"):
        LoggingConfig(level="VERBOSE")


def test_logging_format_validation() -> None:
    """LoggingConfig validates format against json and text."""
    from algodesk.config.models import LoggingConfig

    cfg = LoggingConfig(format="TEXT")
    assert cfg.format == "text"

    with pytest.raises(ValidationError, match="Invalid log format"):
        LoggingConfig(format="xml")


def test_run_config_capital_positive() -> None:
    """allocated_capital must be strictly greater than zero."""
    from algodesk.config.models import RunConfig

    data = _make_valid_run_dict()
    data["allocated_capital"] = 0
    with pytest.raises(ValidationError, match="allocated_capital must be greater than 0"):
        RunConfig.model_validate(data)

    data["allocated_capital"] = -50000
    with pytest.raises(ValidationError, match="allocated_capital must be greater than 0"):
        RunConfig.model_validate(data)

    data["allocated_capital"] = 50000
    cfg = RunConfig.model_validate(data)
    assert cfg.allocated_capital == Decimal("50000")


def test_universe_screen_only_valid() -> None:
    """Universe with only screen is valid."""
    from algodesk.config.models import UniverseConfig

    u = UniverseConfig(screen="nifty_50")
    assert u.screen == "nifty_50"
    assert u.symbols is None


def test_universe_symbols_only_valid() -> None:
    """Universe with only symbols list is valid."""
    from algodesk.config.models import UniverseConfig

    u = UniverseConfig(symbols=["TCS", "INFY"])
    assert u.screen is None
    assert u.symbols == ["TCS", "INFY"]


def test_universe_both_screen_and_symbols_rejected() -> None:
    """Universe with both screen and symbols is rejected."""
    from algodesk.config.models import UniverseConfig

    with pytest.raises(ValidationError, match="Universe requires exactly one"):
        UniverseConfig(screen="nifty_50", symbols=["RELIANCE"])


def test_universe_neither_screen_nor_symbols_rejected() -> None:
    """Universe with neither screen nor symbols is rejected."""
    from algodesk.config.models import UniverseConfig

    with pytest.raises(ValidationError, match="Universe requires exactly one"):
        UniverseConfig()


def test_empty_strategies_rejected() -> None:
    """RunConfig requires at least one strategy."""
    from algodesk.config.models import RunConfig

    data = _make_valid_run_dict()
    # pyrefly: ignore[implicit-any-empty-container]
    data["strategies"] = []
    with pytest.raises(ValidationError, match="At least one strategy"):
        RunConfig.model_validate(data)


def test_valid_run_config() -> None:
    """Valid RunConfig per Appendix C instantiates correctly."""
    from algodesk.config.models import RunConfig

    data = _make_valid_run_dict()
    cfg = RunConfig.model_validate(data)
    assert cfg.allocated_capital == Decimal("100000")
    assert cfg.run.name == "demo_paper"
    assert cfg.run.mode is Mode.PAPER
    assert cfg.data_source.plugin == "upstox"
    assert cfg.execution_venue.plugin == "sim"
    assert cfg.execution_venue.model_extra is not None
    assert cfg.execution_venue.model_extra["fill_model"] == "conservative_book"
    assert cfg.fees.schedule == "zerodha_nse"
    assert cfg.deployment.profile == "local"
    assert cfg.universe.screen == "nifty_liquid_100"
    assert len(cfg.strategies) == 1
    assert len(cfg.notifiers) == 1
