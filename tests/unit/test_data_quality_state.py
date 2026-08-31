from __future__ import annotations

from gugu.data.quality_state import (
    DataQualityState,
    buy_allowed,
    normalize_data_quality,
    resolve_signal_data_quality,
    should_block_buy,
)


def test_quality_state_normalizes_legacy_labels() -> None:
    assert normalize_data_quality("fresh") == DataQualityState.FRESH_VALID
    assert normalize_data_quality("degraded") == DataQualityState.STALE_DEGRADED
    assert normalize_data_quality("invalid") == DataQualityState.UNAVAILABLE


def test_unknown_quality_is_fail_closed() -> None:
    assert normalize_data_quality("made_up") == DataQualityState.UNAVAILABLE
    assert buy_allowed("made_up") is False


def test_unavailable_metadata_overrides_fresh_label() -> None:
    assert normalize_data_quality("fresh", data_available=False) == DataQualityState.UNAVAILABLE
    blocked, reason = should_block_buy(
        {"direction": "buy", "data_quality": "validated", "data_available": False}
    )
    assert blocked is True
    assert "unavailable" in reason


def test_stale_metadata_overrides_fresh_label() -> None:
    assert normalize_data_quality("validated", data_stale=True) == DataQualityState.STALE_DEGRADED


def test_manager_degradation_downgrades_fresh_signal() -> None:
    signal = {"price_is_realtime": True}
    assert resolve_signal_data_quality(signal, dm_degraded=True) == DataQualityState.STALE_DEGRADED


def test_only_fresh_valid_allows_buy() -> None:
    blocked, reason = should_block_buy({"data_quality": "stale_degraded"})
    assert blocked is True
    assert "观察" in reason

    blocked, reason = should_block_buy({"data_quality": "fresh_valid"})
    assert blocked is False
    assert reason == ""


def test_sell_is_not_blocked_by_buy_gate() -> None:
    blocked, reason = should_block_buy({"direction": "sell", "data_quality": "unavailable"})
    assert blocked is False
    assert reason == ""
