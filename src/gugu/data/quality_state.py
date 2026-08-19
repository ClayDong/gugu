"""统一数据质量状态与买入/卖出门禁。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class DataQualityState(StrEnum):
    """交易链路使用的三态数据质量状态。"""

    FRESH_VALID = "fresh_valid"
    STALE_DEGRADED = "stale_degraded"
    UNAVAILABLE = "unavailable"


_BUY_ALLOWED_STATES = {DataQualityState.FRESH_VALID}


def normalize_data_quality(
    value: Any,
    *,
    data_available: bool | None = None,
    data_stale: bool = False,
) -> DataQualityState:
    """将历史数据质量标签归一化为三态状态。"""
    if value in {DataQualityState.FRESH_VALID, "fresh", "validated"}:
        return DataQualityState.FRESH_VALID
    if value in {
        DataQualityState.STALE_DEGRADED,
        "stale",
        "degraded",
        "partial",
    }:
        return DataQualityState.STALE_DEGRADED
    if value in {DataQualityState.UNAVAILABLE, "invalid", "empty"}:
        return DataQualityState.UNAVAILABLE
    if data_available is False:
        return DataQualityState.UNAVAILABLE
    if data_stale:
        return DataQualityState.STALE_DEGRADED
    return DataQualityState.UNAVAILABLE


def resolve_signal_data_quality(
    signal: dict[str, Any],
    *,
    dm_degraded: bool = False,
) -> DataQualityState:
    """从信号及数据管理器状态推导最终交易数据状态。"""
    explicit = signal.get("data_quality")
    if explicit:
        state = normalize_data_quality(
            explicit,
            data_available=signal.get("data_available"),
            data_stale=bool(signal.get("data_stale", False)),
        )
    else:
        inferred_value = signal.get("quality_status")
        if inferred_value is None and signal.get("price_is_realtime"):
            inferred_value = (
                DataQualityState.STALE_DEGRADED if dm_degraded else DataQualityState.FRESH_VALID
            )
        state = normalize_data_quality(
            inferred_value,
            data_available=signal.get("data_available"),
            data_stale=bool(signal.get("data_stale", False)),
        )
    if dm_degraded and state == DataQualityState.FRESH_VALID:
        return DataQualityState.STALE_DEGRADED
    return state


def buy_allowed(state: DataQualityState | str) -> bool:
    """只有 fresh_valid 数据允许新增买入。"""
    try:
        normalized = DataQualityState(state)
    except ValueError:
        return False
    return normalized in _BUY_ALLOWED_STATES


def should_block_buy(signal: dict[str, Any], *, dm_degraded: bool = False) -> tuple[bool, str]:
    """返回新增买入是否应被阻断及原因；卖出不受该门禁影响。"""
    if signal.get("direction") == "sell":
        return False, ""
    state = resolve_signal_data_quality(signal, dm_degraded=dm_degraded)
    if buy_allowed(state):
        return False, ""
    if state == DataQualityState.STALE_DEGRADED:
        return True, "数据状态为 stale_degraded，只允许观察，不允许新增买入"
    return True, "数据状态为 unavailable，禁止新增买入"
