"""四周期模型策略单测（公众号《八月，来了~~》战法）。

覆盖：
1. regime_cycle_four —— 银山谷买入 / 死亡谷卖出 / MACD 状态过滤
2. death_valley_rebound —— 3日不创新低+KDJ金叉+5日线买入
3. bollinger_oversold_rebound —— 布林下轨买入 / 中轨卖出
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.regime_cycle import (
    BollingerOversoldReboundStrategy,
    DeathValleyReboundStrategy,
    RegimeCycleFourStrategy,
)


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
    # 弱反弹中 MACD 大概率死叉 → 不应有大量买入
    assert (out["signal"] == 1).sum() <= 1, "死叉状态不应触发买入"


def test_regime_cycle_confidence_zero_when_no_signal() -> None:
    """无信号时置信度必须为 0。"""
    closes = [100.0] * 60  # 完全横盘
    df = _make_df(closes)
    strategy = RegimeCycleFourStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] != 0).sum() == 0
    assert (out["confidence"] == 0).all()


# ========== death_valley_rebound ==========


def test_death_valley_rebound_buy_on_no_new_low_kdj() -> None:
    """3日不创新低 + KDJ金叉 + 站上5日线（幻灭期+超跌）→ 买入。"""
    # 构造超跌后企稳：长期下跌（>60根让MA60可用）→ 急跌 → 企稳反弹
    closes = (
        list(np.linspace(100, 80, 60))
        + list(np.linspace(80, 55, 25))
        + [54, 54.3, 54.6, 55, 55.4, 55.8, 56.2, 56.6]
    )
    df = _make_df(closes)
    strategy = DeathValleyReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == 1).sum() >= 1, "企稳反弹应触发买入"


def test_death_valley_rebound_sell_below_ma5() -> None:
    """跌破5日均线 → 卖出（反弹结束）。"""
    # 先反弹后回落：站上5日线买入，随后跌破 → 卖出
    closes = (
        list(np.linspace(100, 60, 40))
        + list(np.linspace(58, 70, 10))
        + list(np.linspace(70, 62, 10))
    )
    df = _make_df(closes)
    strategy = DeathValleyReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == -1).sum() >= 1, "跌破5日线应触发卖出"


def test_death_valley_rebound_no_buy_in_freefall() -> None:
    """自由落体（每日创新低）→ 不买入（3日不创新低条件未满足）。"""
    closes = list(np.linspace(100, 50, 50))
    df = _make_df(closes)
    strategy = DeathValleyReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == 1).sum() == 0, "持续创新低不应买入"


def test_death_valley_rebound_no_buy_without_oversold() -> None:
    """无超跌前提（跌幅<10%）→ 不买入（原文：幻灭期+超跌才有修复反弹）。"""
    # 温和下跌后企稳：累计跌幅仅 5%，不满足超跌前提
    closes = list(np.linspace(100, 95, 40)) + [94.5, 94.8, 95.0, 95.2, 95.5, 95.8, 96, 96.2]
    df = _make_df(closes)
    strategy = DeathValleyReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == 1).sum() == 0, "跌幅不足不应触发超跌反弹"


def test_death_valley_rebound_requires_death_valley() -> None:
    """多头排列（MA5>MA60，非幻灭期）→ 不买入。"""
    # 持续上涨：MA5 恒 > MA60，非幻灭期
    closes = list(np.linspace(100, 150, 80))
    df = _make_df(closes)
    strategy = DeathValleyReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == 1).sum() == 0, "多头排列（非幻灭期）不应触发修复反弹"


def test_death_valley_rebound_confidence_bounds() -> None:
    """置信度必须在 [0, 1] 区间。"""
    closes = list(np.linspace(100, 60, 40)) + [58, 58.5, 59, 59.5, 60, 60.5, 61, 61.5, 62]
    df = _make_df(closes)
    strategy = DeathValleyReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert out["confidence"].between(0, 1).all(), "置信度超出 [0,1]"


# ========== bollinger_oversold_rebound ==========


def test_bollinger_oversold_buy_below_lower() -> None:
    """收盘跌破布林下轨 → 买入（超跌抢反弹）。"""
    # 构造急跌：收盘跌破下轨
    closes = list(np.linspace(100, 100, 30)) + list(np.linspace(100, 70, 20))
    df = _make_df(closes)
    strategy = BollingerOversoldReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == 1).sum() >= 1, "跌破下轨应触发买入"


def test_bollinger_oversold_sell_at_mid() -> None:
    """反弹到布林中轨 → 卖出（原文：中轨是反弹目标位）。"""
    # 先跌破下轨，再反弹回中轨
    closes = (
        list(np.linspace(100, 100, 30))
        + list(np.linspace(100, 65, 20))
        + list(np.linspace(65, 100, 20))
    )
    df = _make_df(closes)
    strategy = BollingerOversoldReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == -1).sum() >= 1, "反弹到中轨应触发卖出"


def test_bollinger_oversold_no_buy_in_normal_range() -> None:
    """单调上涨（close 恒在均线上方）→ 不触发买入。"""
    n = 80
    closes = list(np.linspace(100, 120, n))  # 平稳上行，永不跌破下轨
    df = _make_df(closes)
    strategy = BollingerOversoldReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == 1).sum() == 0, "上行趋势未跌破2σ下轨不应触发买入"


def test_bollinger_oversold_confidence_bounds() -> None:
    """置信度必须在 [0, 1] 区间。"""
    closes = list(np.linspace(100, 100, 30)) + list(np.linspace(100, 70, 20))
    df = _make_df(closes)
    strategy = BollingerOversoldReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert out["confidence"].between(0, 1).all(), "置信度超出 [0,1]"


def test_bollinger_oversold_requires_drop_threshold() -> None:
    """超跌前提：累计跌幅不足阈值 → 不触发买入（防普通波动跌破下轨误触发）。"""
    # 20日累计跌幅仅 6%（<12% 阈值）→ 即使跌破下轨也不买
    closes = list(np.linspace(100, 100, 30)) + list(np.linspace(100, 94, 20))
    df = _make_df(closes)
    strategy = BollingerOversoldReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == 1).sum() == 0, "跌幅不足12%不应触发超跌买入"


def test_bollinger_oversold_triggered_with_deep_drop() -> None:
    """超跌前提：20日累计跌幅 >12% + 跌破下轨（深跌破位）→ 触发买入。"""
    # 从 100 急跌到 65（-35%），布林带下轨因 std 扩大下移，需深跌才破位
    closes = list(np.linspace(100, 100, 30)) + list(np.linspace(100, 65, 20))
    df = _make_df(closes)
    strategy = BollingerOversoldReboundStrategy()
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == 1).sum() >= 1, "深度超跌应触发买入"
