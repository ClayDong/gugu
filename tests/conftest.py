"""测试共享 fixture。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def isolate_project_paths(tmp_path, monkeypatch):
    """隔离测试与生产数据目录：所有测试默认写入 tmp_path。

    防止测试污染生产环境的 data/heartbeat.json、signals_history.jsonl、
    paper_broker_state.json、risk_state.json 等可观测性文件。
    """
    monkeypatch.setattr("gugu.engine.main.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("gugu.risk.manager.RISK_STATE_FILE", tmp_path / "risk_state.json")


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
