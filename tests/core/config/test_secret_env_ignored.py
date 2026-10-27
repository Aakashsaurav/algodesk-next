"""Test that ALGODESK_SECRET_* environment variables are ignored by config loader."""

from __future__ import annotations

from algodesk.config.loader import _extract_env_layers, load_platform


def test_extract_env_layers_ignores_algodesk_secret_variables() -> None:
    env = {
        "ALGODESK_RUN__NAME": "test_run",
        "ALGODESK_ALLOCATED_CAPITAL": "250000",
        "ALGODESK_SECRET_TOKEN": "super_secret_token_value",
        "ALGODESK_SECRET_UPSTOX_KEY": "secret_key_123",
        "ALGODESK_SECRET_PASSWORD": "do_not_load_in_config",
        "OTHER_ENV_VAR": "ignored_anyway",
    }
    layers = _extract_env_layers(env)

    layer_names = [name for name, _ in layers]
    assert "env:ALGODESK_RUN__NAME" in layer_names
    assert "env:ALGODESK_ALLOCATED_CAPITAL" in layer_names

    # Assert none of the ALGODESK_SECRET_* are present in config layers
    assert not any("ALGODESK_SECRET_" in name for name in layer_names)

    # Check combined dictionary content
    combined: dict[str, object] = {}
    for _, layer_dict in layers:
        combined.update(layer_dict)

    assert "secret_token" not in combined
    assert "secret_upstox_key" not in combined
    assert "secret_password" not in combined
    assert "secret" not in combined


def test_load_platform_ignores_secret_env_vars() -> None:
    from pathlib import Path

    env = {
        "ALGODESK_SECRET_KEY": "secret_value",
        "ALGODESK_SECRET_FOO": "bar",
    }
    cfg = load_platform(root=Path("."), env=env)
    assert not hasattr(cfg, "secret_key")
    assert not hasattr(cfg, "secret_foo")
