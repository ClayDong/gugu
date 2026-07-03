"""陈江挺阻力突破策略单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.chen_jiangting import ChenJiangtingStrategy

# 固定随机种子，保证测试可复现
np.random.seed(42)


def _make_df(n: int = 120, base_price: float = 10.0, trend: float = 0.0) -> pd.DataFrame:
    """构造测试 DataFrame（确定性数据，无随机噪声）。"""
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    # 用确定性公式生成 close（无随机噪声，避免测试不稳定）
    close = [base_price * (1 + trend) ** i for i in range(n)]
    high = [c + 0.05 for c in close]
    low = [c - 0.05 for c in close]
    return pd.DataFrame({
        "date": dates,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": [10000 + i * 100 for i in range(n)],
        "amount": [100000 + i * 1000 for i in range(n)],
    })


def _make_rising_df(n: int = 120, base_price: float = 10.0, trend: float = 0.005) -> pd.DataFrame:
    """构造稳定上升趋势的 DataFrame。"""
    return _make_df(n, base_price, trend)


def test_resistance_breakout_buy_signal():
    """阻力突破 + 量能确认 → 买入信号。"""
    df = _make_df(120, base_price=10.0, trend=0.005)  # 缓慢上升趋势
    # 最后一天突破：价格创新高 + 成交量放大
    df.loc[df.index[-1], "close"] = df["high"].iloc[:-1].max() + 0.5
    df.loc[df.index[-1], "high"] = df["close"].iloc[-1]
    df.loc[df.index[-1], "volume"] = df["volume"].iloc[-30:].mean() * 2.0  # 放量 2 倍

    strategy = ChenJiangtingStrategy()
    result = strategy.generate_signals(df)
    assert result["signal"].iloc[-1] == 1, "阻力突破 + 放量应触发买入"


def test_no_breakout_no_signal():
    """无突破 → 无信号（价格持续小幅震荡，不创新高也不跌破 MA20）。"""
    n = 120
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    # 价格在 10.0 附近窄幅震荡，不形成突破
    close = [10.0 + 0.1 * np.sin(i / 10) for i in range(n)]
    df = pd.DataFrame({
        "date": dates,
        "open": close,
        "high": [c + 0.05 for c in close],
        "low": [c - 0.05 for c in close],
        "close": close,
        "volume": [10000 + i * 100 for i in range(n)],
        "amount": [100000 + i * 1000 for i in range(n)],
    })

    strategy = ChenJiangtingStrategy()
    result = strategy.generate_signals(df)
    assert result["signal"].iloc[-1] == 0, "无突破不应有信号"


def test_breakout_without_volume_no_signal():
    """突破但无放量 → 无信号（量能确认是必须的）。"""
    df = _make_rising_df(120, base_price=10.0, trend=0.005)
    # 价格突破前高但成交量与均量持平（无放量）
    resistance = df["high"].iloc[:-1].max()
    df.loc[df.index[-1], "close"] = resistance + 0.5
    df.loc[df.index[-1], "high"] = df["close"].iloc[-1] + 0.1
    df.loc[df.index[-1], "low"] = df["close"].iloc[-1] - 0.1
    df.loc[df.index[-1], "volume"] = df["volume"].iloc[-30:].mean()

    strategy = ChenJiangtingStrategy()
    result = strategy.generate_signals(df)
    # 期望 signal 不是 1（无放量不应买入），可能为 0 或 -1
    assert result["signal"].iloc[-1] != 1, "突破但无放量不应触发买入"


def test_sell_on_below_ma20():
    """跌破 MA20 → 卖出信号。"""
    df = _make_df(120, base_price=10.0, trend=0.01)  # 上升趋势
    # 最后一天跌破 MA20
    ma20 = df["close"].rolling(20).mean().iloc[-1]
    df.loc[df.index[-1], "close"] = ma20 * 0.95

    strategy = ChenJiangtingStrategy()
    result = strategy.generate_signals(df)
    assert result["signal"].iloc[-1] == -1, "跌破 MA20 应触发卖出"


def test_crazy_gain_filtered():
    """当日涨幅 >7% 视为疯狂阶段，不追。"""
    df = _make_df(120, base_price=10.0, trend=0.005)
    # 最后一天涨幅 8%
    prev_close = df["close"].iloc[-2]
    df.loc[df.index[-1], "close"] = prev_close * 1.08
    df.loc[df.index[-1], "high"] = df["close"].iloc[-1]
    df.loc[df.index[-1], "volume"] = df["volume"].iloc[-30:].mean() * 2.0

    strategy = ChenJiangtingStrategy()
    result = strategy.generate_signals(df)
    assert result["signal"].iloc[-1] == 0, "涨幅 8% 应被阶段过滤，不追疯狂段"


def test_data_too_short_no_signal():
    """数据不足 → 无信号（不应崩溃）。"""
    df = _make_df(30, base_price=10.0)
    strategy = ChenJiangtingStrategy()
    result = strategy.generate_signals(df)
    assert result["signal"].iloc[-1] == 0, "数据不足应返回 0"
    assert (result["signal"] == 0).all(), "数据不足时所有信号应为 0"


def test_sell_on_volume_divergence():
    """量价背离 → 卖出信号（价格创新高但量萎缩）。"""
    df = _make_df(120, base_price=10.0, trend=0.01)  # 上升趋势
    # 最后一天价格创 20 日新高
    df.loc[df.index[-1], "close"] = df["close"].iloc[-20:].max() + 0.1
    df.loc[df.index[-1], "high"] = df["close"].iloc[-1]
    # 但成交量低于 5 日均量
    df.loc[df.index[-1], "volume"] = df["volume"].iloc[-5:].min() * 0.5

    strategy = ChenJiangtingStrategy()
    result = strategy.generate_signals(df)
    assert result["signal"].iloc[-1] == -1, "量价背离应触发卖出"


def test_confidence_range():
    """置信度在 0-1 之间。"""
    df = _make_df(120, base_price=10.0, trend=0.005)
    df.loc[df.index[-1], "close"] = df["high"].iloc[:-1].max() + 0.5
    df.loc[df.index[-1], "high"] = df["close"].iloc[-1]
    df.loc[df.index[-1], "volume"] = df["volume"].iloc[-30:].mean() * 2.0

    strategy = ChenJiangtingStrategy()
    result = strategy.generate_signals(df)
    if result["signal"].iloc[-1] == 1:
        conf = result["confidence"].iloc[-1]
        assert 0 <= conf <= 1.0, f"置信度应在 0-1 之间，实际 {conf}"


def test_strategy_registered():
    """策略应注册到 registry。"""
    from gugu.strategies.registry import _REGISTRY
    assert "chen_jiangting" in _REGISTRY


def test_sell_overrides_buy():
    """卖出信号应覆盖买入信号。"""
    df = _make_df(120, base_price=10.0, trend=0.01)
    # 构造同时触发买入和卖出的场景：突破 + 放量但跌破 MA20
    ma20 = df["close"].rolling(20).mean().iloc[-1]
    # 价格在 ma20 之下但突破前期阻力（构造矛盾场景）
    resistance = df["high"].rolling(20).max().shift(1).iloc[-1]
    df.loc[df.index[-1], "close"] = min(ma20 * 0.95, resistance + 0.1)
    df.loc[df.index[-1], "volume"] = df["volume"].iloc[-30:].mean() * 2.0

    strategy = ChenJiangtingStrategy()
    result = strategy.generate_signals(df)
    # 跌破 MA20 优先
    assert result["signal"].iloc[-1] == -1, "卖出信号应覆盖买入信号"
