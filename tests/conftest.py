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

    P1-2 修复（基于对抗性审查）：
    - L1: paper.STATE_FILE 未重定向 → PaperBroker 实例化即读写生产 data/paper_broker_state.json
    - L2: gugu.config.PROJECT_ROOT 未重定向 → signal_pipeline.record_signal_history
          用的 gugu_config.PROJECT_ROOT 与 engine.main.PROJECT_ROOT 是不同模块的引用
    - L3: record_signal_history 未全局 mock → 除 test_engine.py 外的 40+ 测试会触发写入
    - L4: TradingEngine._notified_signals_file 在 __init__ 中即计算路径
    """
    monkeypatch.setattr("gugu.engine.main.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("gugu.config.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("gugu.risk.manager.RISK_STATE_FILE", tmp_path / "risk_state.json")
    monkeypatch.setattr("gugu.execution.paper.STATE_FILE", tmp_path / "paper_broker_state.json")
    # 屏蔽 signals_history.jsonl 写入（测试用例无需关心历史记录持久化）
    monkeypatch.setattr(
        "gugu.engine.main.record_signal_history", lambda *a, **kw: None
    )
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
