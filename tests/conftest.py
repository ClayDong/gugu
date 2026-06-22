"""测试共享 fixture。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def ohlcv_df() -> pd.DataFrame:
    """构造一段 60 日的 OHLCV 测试数据，无 NaN。"""
    np.random.seed(42)
    n = 60
    base = 100.0
    close = base + np.cumsum(np.random.randn(n) * 2)
    high = close + np.random.rand(n) * 3
    low = close - np.random.rand(n) * 3
    open_ = low + np.random.rand(n) * (high - low)
    volume = np.random.randint(1_000_000, 10_000_000, size=n)
    amount = close * volume
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )
