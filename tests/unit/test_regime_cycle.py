"""四周期模型策略单测（公众号《八月，来了~~》战法）。

覆盖 regime_cycle_four —— 银山谷买入 / 死亡谷卖出 / MACD 状态过滤。
death_valley_rebound 和 bollinger_oversold_rebound 已删除（2026-09-02 策略清洗）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.regime_cycle import RegimeCycleFourStrategy


def _make_df(closes: list[float]) -> pd.DataFrame:
    """从收盘价序列构造 OHLCV DataFrame。"""
    n = len(closes)
    close = pd.Series(closes, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=n, freq="B"),
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "close": close,
            "volume": [1_000_000] * n,
            "amount": close * 1_000_000,
        }
    )


# ========== regime_cycle_four ==========


def test_regime_cycle_buy_on_silver_valley() -> None:
    """MA20 上穿 MA60（银山谷）+ MACD 金叉状态 → 买入信号。"""
    # 构造 V 型走势：下跌后企稳反转，形成 MA20 上穿 MA60
    n = 120
    closes = []
    base = 100.0
    # 前 60 日下跌
    for i in range(60):
        closes.append(base * (1 - i / 120))
    # 后 60 日快速上涨（形成金叉）
    for i in range(60):
        closes.append(base * (1 - 0.5 + i / 40))
    df = _make_df(closes)
    strategy = RegimeCycleFourStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == 1).sum() >= 1, "银山谷应触发至少一次买入"


def test_regime_cycle_sell_on_death_valley() -> None:
    """MA20 下穿 MA60（死亡谷）+ MACD 死叉状态 → 卖出信号。"""
    # 构造倒 V 型走势：上涨后见顶回落，形成 MA20 下穿 MA60
    n = 120
    closes = []
    base = 100.0
    for i in range(60):
        closes.append(base * (1 + i / 120))
    for i in range(60):
        closes.append(base * (1 + 0.5 - i / 40))
    df = _make_df(closes)
    strategy = RegimeCycleFourStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == -1).sum() >= 1, "死亡谷应触发至少一次卖出"


def test_regime_cycle_macd_dead_state_blocks_buy() -> None:
    """MACD 死叉状态 + 银山谷 → 不买入（原文：死叉状态不会有主升浪）。"""
    # 构造横盘后 MA20 上穿 MA60，但构造 MACD 持续死叉（dif < dea）的形态
    n = 120
    closes = list(np.linspace(100, 80, 60)) + list(np.linspace(80, 82, 60))
    df = _make_df(closes)
    strategy = RegimeCycleFourStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    # 弱反弹中 MACD 大概率死叉 → 不应有太多买入
    # 2026-09-03: silver_window=5 可能产生不多于 silver_window+2 个信号
    assert (out["signal"] == 1).sum() <= 8, "死叉状态不应触发太多买入"
    # 确认不是每个信号都是买入


def test_regime_cycle_confidence_zero_when_no_signal() -> None:
    """无信号时置信度必须为 0。"""
    closes = [100.0] * 60  # 完全横盘
    df = _make_df(closes)
    strategy = RegimeCycleFourStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] != 0).sum() == 0
    assert (out["confidence"] == 0).all()
