"""数据证据链元数据工具。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

DEFAULT_METADATA: dict[str, Any] = {
    "data_source": "unknown",
    "fetch_timestamp": "",
    "period": "",
    "unit": "",
    "adjust_type": "none",
    "quality_status": "unknown",
    "data_stale": False,
}


def set_evidence(
    df: pd.DataFrame,
    *,
    data_source: str,
    period: str = "",
    unit: str = "",
    adjust_type: str = "none",
    quality_status: str = "validated",
    fetch_timestamp: str | None = None,
    data_stale: bool = False,
    data_available: bool | None = None,
    data_error: str | None = None,
) -> pd.DataFrame:
    """原地补齐 DataFrame 证据链并返回自身。"""
    if not data_source.strip():
        raise ValueError("data_source 不能为空")
    df.attrs.update(
        {
            "data_source": data_source,
            "fetch_timestamp": fetch_timestamp or datetime.now().isoformat(timespec="seconds"),
            "period": period,
            "unit": unit,
            "adjust_type": adjust_type,
            "quality_status": quality_status,
            "data_stale": data_stale,
        }
    )
    if data_available is not None:
        df.attrs["data_available"] = data_available
    if data_error is not None:
        df.attrs["data_error"] = data_error
    return df


def copy_evidence(source: pd.DataFrame, target: pd.DataFrame) -> pd.DataFrame:
    """将来源 DataFrame 的证据链复制到目标 DataFrame。"""
    for key in DEFAULT_METADATA:
        if key in source.attrs:
            target.attrs[key] = source.attrs[key]
    for key in ("data_available", "data_error", "all_sources_failed"):
        if key in source.attrs:
            target.attrs[key] = source.attrs[key]
    return target
