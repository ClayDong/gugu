"""数据层单元测试。"""
from __future__ import annotations

import pandas as pd
import pytest

from gugu.data.cache import DataCache, cache
from gugu.data.quality import (
    DataQualityError,
    validate_sector_flow,
    validate_stock_flow,
    validate_stock_history,
)


def test_cache_set_get() -> None:
    c = DataCache(ttl_seconds=120)
    c.set("key", "value")
    assert c.get("key") == "value"


def test_cache_expired() -> None:
    c = DataCache(ttl_seconds=-1)
    c.set("key", "value")
    assert c.get("key") is None


def test_cache_clear() -> None:
    c = DataCache()
    c.set("key", "value")
    c.clear()
    assert c.get("key") is None
    assert c.stats()["keys"] == 0


def test_cache_global_singleton() -> None:
    assert isinstance(cache(), DataCache)


def test_validate_stock_history_ok() -> None:
    df = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=5),
            "open": [10.0] * 5,
            "high": [11.0] * 5,
            "low": [9.0] * 5,
            "close": [10.5] * 5,
            "volume": [1000] * 5,
        }
    )
    out = validate_stock_history(df, "600519")
    assert len(out) == 5


def test_validate_stock_history_missing_column() -> None:
    df = pd.DataFrame({"date": [1, 2, 3]})
    with pytest.raises(DataQualityError, match="缺失列"):
        validate_stock_history(df, "600519")


def test_validate_stock_history_negative_values() -> None:
    df = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=3),
            "open": [10.0, 10.0, 10.0],
            "high": [11.0, 11.0, 11.0],
            "low": [9.0, 9.0, 9.0],
            "close": [10.5, -1.0, 10.5],
            "volume": [1000, 1000, 1000],
        }
    )
    out = validate_stock_history(df, "600519")
    assert len(out) == 2


def test_validate_stock_history_high_low_invalid() -> None:
    df = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=3),
            "open": [10.0] * 3,
            "high": [11.0, 8.0, 11.0],
            "low": [9.0] * 3,
            "close": [10.5] * 3,
            "volume": [1000] * 3,
        }
    )
    out = validate_stock_history(df, "600519")
    assert len(out) == 2


def test_validate_sector_flow_ok() -> None:
    df = pd.DataFrame(
        {
            "sector": ["白酒", "白酒", "银行"],
            "main_net": [1e8, 2e8, 3e8],
            "main_pct": [0.1, 0.2, 0.3],
        }
    )
    out = validate_sector_flow(df)
    assert len(out) == 2


def test_validate_sector_flow_missing_column() -> None:
    df = pd.DataFrame({"sector": ["白酒"]})
    with pytest.raises(DataQualityError, match="缺失列"):
        validate_sector_flow(df)


def test_validate_stock_flow_ok() -> None:
    df = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=3),
            "main_net": [1e6, 2e6, 3e6],
            "main_pct": [0.01, 0.02, 0.03],
        }
    )
    out = validate_stock_flow(df, "600519")
    assert len(out) == 3


def test_validate_stock_flow_without_date() -> None:
    df = pd.DataFrame({"main_net": [1e6], "main_pct": [0.01]})
    out = validate_stock_flow(df, "600519")
    assert len(out) == 1
