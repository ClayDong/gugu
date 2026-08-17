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
    # L7 (2026-08-10)：source_health 的 _EVENTS_FILE/_SUMMARY_FILE 在模块导入时绑定
    # 生产路径（from gugu.config import PROJECT_ROOT），conftest patch PROJECT_ROOT 属性
    # 不影响它 → 测试进程的真实 DataManager 失败会写生产 data/source_health_events.jsonl，
    # 掺入 watchdog 失败率统计（当日 53-61% 高失败率部分为测试污染）。
    # 单例 _instance 也需重置，否则 __init__ 只在首实例绑定一次旧路径。
    monkeypatch.setattr(
        "gugu.data.source_health._EVENTS_FILE",
        tmp_path / "source_health_events.jsonl",
    )
    monkeypatch.setattr(
        "gugu.data.source_health._SUMMARY_FILE",
        tmp_path / "source_health_daily.json",
    )
    monkeypatch.setattr("gugu.data.source_health.DataSourceHealthMonitor._instance", None)
    # 屏蔽 signals_history.jsonl 写入（测试用例无需关心历史记录持久化）
    monkeypatch.setattr("gugu.engine.main.record_signal_history", lambda *a, **kw: None)
    # L5: signal_pipeline 的告警函数未隔离 → 测试 mock PROJECT_ROOT 为不存在路径时，
    #     真实 record_signal_history 捕获异常后触发 _alert_signal_history_write_failure
    #     daemon 线程会真实推送飞书（P0 测试副作用泄漏到生产通知，2026-08-01 事故）
    monkeypatch.setattr(
        "gugu.engine.signal_pipeline._alert_signal_history_write_failure",
        lambda *a, **kw: None,
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


# ══════════════════════════════════════════════════════════════
# v0.9.11 防线3：真实世界脏数据样本（固化生产实战 bug 场景）
# 来源：2026-08-07 前 gugu 生产实际触发过 bug 的脏数据形态。
# 任何"数据输入"类测试应优先从这些样本取数——mock 全绿 ≠ 生产无 bug。
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def dirty_history_df() -> pd.DataFrame:
    """历史行情含 None/NaN/零值（真实：数据源字段漂移时的形态）。

    触发过的 bug：f-string 格式化 None 崩溃（v0.9.9 信号黑洞）。
    """
    return pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"]
            ),
            "open": [10.0, None, 10.5, float("nan"), 11.0],
            "high": [10.5, 10.8, None, 11.2, 11.5],
            "low": [9.8, None, 10.0, 10.5, None],
            "close": [10.2, 10.4, 10.6, None, 11.2],
            "volume": [1000, None, 1200, 800, float("nan")],
            "amount": [10000.0, 10400.0, 12720.0, None, 12320.0],
        }
    )


@pytest.fixture
def dirty_snapshot_df() -> pd.DataFrame:
    """全市场快照含 None 市值/换手率（真实：新浪/东财字段缺失）。

    触发过的 bug：市值单位不统一（万元 vs 元）、换手率 None 导致过滤误判。
    """
    return pd.DataFrame(
        {
            "symbol": ["600519", "000001", "300750", "920000"],
            "name": ["贵州茅台", "平安银行", "宁德时代", "安徽凤凰"],
            "price": [1500.0, 11.5, 250.0, 14.1],
            "change_pct": [1.2, -0.5, None, 3.4],
            "volume": [100000, 200000, None, 50000],
            "amount": [1e9, 2e8, 3e9, 7e7],
            "turnoverratio": [0.5, 2.0, None, 5.5],
            "circ_mv": [2e12, 3e11, 1.2e12, None],  # 元
        }
    )


@pytest.fixture
def empty_df() -> pd.DataFrame:
    """空 DataFrame（真实：东财限流 RemoteDisconnected 后的返回值）。"""
    return pd.DataFrame()


@pytest.fixture
def none_fields_signal() -> dict:
    """信号 dict 含 None 字段（真实：v0.9.9 视角分析失败路径）。

    触发过的 bug：record_signal_history 的 .get('composite_score', 0) 在值为
    None 时返回 None → f"{None:.1f}" 抛 TypeError → 信号历史 35 天零写入。
    """
    return {
        "symbol": "600519",
        "direction": "buy",
        "price": 1500.0,
        "confidence": 0.425,
        "strategies": ["double_yin"],
        "perspective_check": {
            "composite_score": None,
            "min_score": None,
            "passed": None,
            "fatal_count": 0,
            "verdict": "分析失败（signal_only 模式不拦截）",
        },
        "wisdom_decision": {"source": "fallback"},
    }


@pytest.fixture
def truncated_snapshot_df() -> pd.DataFrame:
    """残缺快照（真实：8/5 东财限流只返回 80 只 vs 正常 5000+）。

    触发过的 bug：候选数骤减导致选股零产出，但系统无告警。
    """
    rows = [
        {
            "symbol": f"600{str(i).zfill(3)}",
            "name": f"测试股{i}",
            "price": 10.0 + i,
            "change_pct": 0.5,
            "volume": 100000,
            "amount": 1e7,
            "turnoverratio": 5.0,
            "circ_mv": 1e10,
        }
        for i in range(80)
    ]
    return pd.DataFrame(rows)
