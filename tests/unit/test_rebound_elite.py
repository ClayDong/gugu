"""rebound_elite 策略单元测试。

验证：断板反包·精英追涨策略的核心行为——高活跃妖股上触发、低活跃股不上、
已触发点带置信度、且能正确注册。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.registry import get_strategy, list_strategies


def make_df(close_list, open_list=None, high_list=None, low_list=None,
            volume_list=None, amount=None):
    """构造 OHLCV 数据帧。"""
    n = len(close_list)
    open_ = open_list or close_list
    high = high_list or [c * 1.02 for c in close_list]
    low = low_list or [c * 0.98 for c in close_list]
    vol = volume_list or [1_000_000] * n
    amt = amount or [v * c for v, c in zip(vol, close_list)]
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close_list, "volume": vol, "amount": amt,
    })


@pytest.fixture
def strategy():
    return get_strategy("rebound_elite")


def test_registered():
    assert "rebound_elite" in list_strategies()


def test_generates_signal_on_active_stock():
    """高活跃股 + 完整断板反包形态应触发买入。

    用低 min_limit_ups 隔离测试形态识别逻辑本身（活跃度过滤由
    test_no_signal_on_low_activity 单独覆盖）。
    """
    strat = get_strategy("rebound_elite", params={"min_limit_ups": 1})
    # 构造：含断板反包形态。关键：首板跳空高开(32.5)，洗盘大阴但最低点不破
    # 首板开盘价支撑32.5×(1-3%)=31.5（护盘有效）
    closes = [10.0] * 30  # 横盘吸筹
    # 首板跳空涨停：10 -> 33 (收于33, 开盘32.5)
    closes += [33.0]
    # 洗盘大阴：32.8开 → 31.4收（跌约5%），low=31.7 不破支撑31.5
    closes += [31.4]
    # 护盘缩量横盘（所有low均≥31.6，不破支撑31.525）
    closes += [31.8, 31.9, 31.7]
    # 反包放量阳线：收盘33.6，突破洗盘日最高价33.0（放量阳线突破）
    closes += [33.6]

    # 开盘/最高/最低价序列（与收盘对齐）
    opens = [10.0] * 30 + [32.5, 32.8, 31.8, 31.9, 31.7, 32.1]
    highs = [10.2] * 30 + [33.0, 33.0, 32.0, 32.0, 31.9, 33.8]
    # 支撑线 = 32.5*(1-3%) = 31.525；护盘期所有 low ≥ 31.6
    lows  = [9.8] * 30 + [32.5, 31.7, 31.6, 31.6, 31.6, 31.6]
    # 量：首板放量，洗盘大放量，护盘缩量，反包放量
    vols = [1_000_000] * 30 + [2_000_000, 3_000_000, 600_000, 600_000, 600_000, 2_500_000]

    df = make_df(closes, open_list=opens, volume_list=vols,
                 high_list=highs, low_list=lows)

    out = strat.generate_signals(df)
    assert "signal" in out.columns
    assert "confidence" in out.columns
    # 形态在反包日应有买入信号
    buy_days = out[out["signal"] == 1]
    assert len(buy_days) >= 1, f"应触发至少1次买入，实际{buy_days}"
    # 置信度在 (0,1]
    assert (buy_days["confidence"] > 0).all()


def test_no_signal_on_low_activity(strategy):
    """活跃度不足的股票（几乎无涨停）不应触发。"""
    # 长期平缓波动，无显著涨停
    rng = np.random.default_rng(42)
    closes = [10.0 + i * 0.01 for i in range(100)]
    df = make_df(closes)
    out = strategy.generate_signals(df)
    assert (out["signal"] == 0).all(), "低活跃股不应触发任何买入"
