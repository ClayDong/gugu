"""测试共享 fixture。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# 将 src 加入 Python 路径，使 pytest 可直接运行（无需 editable install）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _PROJECT_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# P0 修复：通过环境变量标记测试模式，避免生产代码用 "pytest" in sys.modules 检测
os.environ["GUGU_TEST_MODE"] = "1"


@pytest.fixture(autouse=True)
def isolate_project_paths(tmp_path, monkeypatch):
    """隔离测试与生产数据目录：所有测试默认写入 tmp_path。

    防止测试污染生产环境的 data/heartbeat.json、signals_history.jsonl、
    paper_broker_state.json、risk_state.json 等可观测性文件。
    """
    monkeypatch.setattr("gugu.engine.main.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("gugu.risk.manager.RISK_STATE_FILE", tmp_path / "risk_state.json")
    # 通过显式环境变量通知 FeishuNotifier 重定向重试队列（替代 sys.modules 检测）
    monkeypatch.setenv("GUGU_TEST_MODE", "1")


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
