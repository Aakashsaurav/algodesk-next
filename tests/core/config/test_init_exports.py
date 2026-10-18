"""Tests for algodesk.config.__init__ — exports and completeness."""

from __future__ import annotations


def test_config_all_exported_symbols_exist() -> None:
    """Every symbol in __all__ must be resolvable from algodesk.config."""
    import algodesk.config

    for symbol in algodesk.config.__all__:
        assert hasattr(algodesk.config, symbol), f"Symbol '{symbol}' in __all__ not found in config"


def test_config_required_contracts_exported() -> None:
    """All public models and functions must be present in __all__."""
    import algodesk.config

    required = {
        "PlatformConfig",
        "PathsConfig",
        "LoggingConfig",
        "PluginRef",
        "RunConfig",
        "RunMeta",
        "FeesConfig",
        "DeploymentConfig",
        "UniverseConfig",
        "StrategyConfig",
        "load_platform",
        "load_run",
        "load_toml",
        "deep_merge",
        "merge_layers",
    }
    missing = required - set(algodesk.config.__all__)
    assert not missing, f"Missing exports in algodesk.config.__all__: {missing}"
