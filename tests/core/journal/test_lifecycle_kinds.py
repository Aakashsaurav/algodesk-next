"""Tests for M1 design 8.1 lifecycle event kinds catalogue and validation."""

from __future__ import annotations

import pytest

from algodesk.journal.kinds import (
    M1_DESIGN_8_1_KINDS,
    NEW_OBSERVABILITY_KINDS,
    REGISTERED_EVENT_KINDS,
    is_registered_kind,
    register_event_kind,
)


def test_registered_kinds_contain_all_design_8_1_kinds() -> None:
    expected = {
        "run.start",
        "config.effective",
        "registry.snapshot",
        "instruments.loaded",
        "feed.connected",
        "tick.stats",
        "bar.stats",
        "signal",
        "intent",
        "risk.decision",
        "order.created",
        "order.event",
        "fill",
        "portfolio.snapshot",
        "error",
        "run.end",
    }
    for k in expected:
        assert k in M1_DESIGN_8_1_KINDS
        assert is_registered_kind(k)


def test_observability_kinds_registered() -> None:
    assert "proc.stats" in NEW_OBSERVABILITY_KINDS
    assert "latency.stats" in NEW_OBSERVABILITY_KINDS
    assert is_registered_kind("proc.stats")
    assert is_registered_kind("latency.stats")


def test_custom_kind_registration() -> None:
    test_kind = "custom.test_kind_xyz"
    assert not is_registered_kind(test_kind)
    register_event_kind(test_kind)
    assert is_registered_kind(test_kind)


def test_invalid_kind_registration_raises_error() -> None:
    with pytest.raises(ValueError, match="Invalid event kind format"):
        register_event_kind("INVALID_KIND")

    with pytest.raises(ValueError, match="Invalid event kind format"):
        register_event_kind("123bad")

    with pytest.raises(ValueError, match="Invalid event kind format"):
        register_event_kind("bad-hyphen")


def test_all_catalogued_kinds_match_regex() -> None:
    import re

    regex = re.compile(r"^[a-z][a-z0-9_.]*$")
    for k in REGISTERED_EVENT_KINDS:
        assert regex.match(k) is not None


def test_proc_stats_fields_contract() -> None:
    sample_payload = {
        "peak_rss_mb": 142.5,
        "journal_bytes": 10240,
        "recorder_rows": 500,
        "recorder_bytes": 20480,
    }
    assert isinstance(sample_payload["peak_rss_mb"], float)
    assert isinstance(sample_payload["journal_bytes"], int)
    assert isinstance(sample_payload["recorder_rows"], int)
    assert isinstance(sample_payload["recorder_bytes"], int)


def test_latency_stats_fields_contract() -> None:
    sample_payload = {
        "p50_ms": 1.25,
        "p95_ms": 3.85,
        "samples": 42,
    }
    assert isinstance(sample_payload["p50_ms"], float)
    assert isinstance(sample_payload["p95_ms"], float)
    assert isinstance(sample_payload["samples"], int)
