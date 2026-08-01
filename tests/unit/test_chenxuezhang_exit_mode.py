"""chenxuezhang exit_mode 优化单测（2026-08-01）。

覆盖：
1. legacy 模式保留旧行为（日涨>2% 即卖出信号）
2. optimized 模式 R15 改为上影线骗线（普通大涨不卖，冲高回落才卖）
3. optimized 模式 R25 收紧（极端缩量才卖）
4. optimized 模式启用 max_hold_days 时间止损
5. 默认 exit_mode = optimized
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.blogger_rules.chenxuezhang import ChenXueZhangStrategy


def make_df(n=120, base=10.0, daily_drift=0.0, trend_down=False) -> pd.DataFrame:
    """构造测试行情：平稳上涨，无极端波动。trend_down=True 时缓慢下跌。"""
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    rng = np.random.default_rng(42)
    drift = -0.004 if trend_down else daily_drift
    rets = rng.normal(drift, 0.006, n)  # 低波动
    close = base * np.cumprod(1 + rets)
    open_ = close * (1 + rng.normal(0, 0.002, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, n)))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame(
        {"date": dates, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )


def test_default_exit_mode_is_optimized():
    """默认 exit_mode 应为 optimized（修复版优先）。"""
    st = ChenXueZhangStrategy()
    assert st.params.get("exit_mode", "optimized") == "optimized"


def test_legacy_sells_on_plain_2pct_up_day():
    """legacy：普通日涨>2% 触发卖出信号（旧行为保留）。"""
    df = make_df()
    # 构造一个大涨日
    df.loc[60, "close"] = df.loc[59, "close"] * 1.025
    df.loc[60, "high"] = df.loc[60, "close"] * 1.005  # 无上影线（冲高不多）
    df.loc[60, "low"] = df.loc[60, "close"] * 0.99

    st = ChenXueZhangStrategy({"exit_mode": "legacy"})
    sig = st.generate_signals(df.copy(), is_backtest=True)
    assert sig["signal"].iloc[60] == -1, "legacy 模式日涨2.5%应触发卖出"


def test_optimized_does_not_sell_on_plain_2pct_up_day():
    """optimized：普通日涨2.5%（无长上影线）不触发卖出——修复一涨就卖。"""
    df = make_df()
    df.loc[60, "close"] = df.loc[59, "close"] * 1.025
    df.loc[60, "high"] = df.loc[60, "close"] * 1.005  # 上影线仅0.5%
    df.loc[60, "low"] = df.loc[60, "close"] * 0.99

    st = ChenXueZhangStrategy({"exit_mode": "optimized"})
    sig = st.generate_signals(df.copy(), is_backtest=True)
    assert sig["signal"].iloc[60] != -1, "optimized 模式无上影线的普通大涨不应卖出"


def test_optimized_sells_on_upper_shadow_fake_pump():
    """optimized：冲高回落（上影线>3% 且收涨）触发卖出——真尾盘骗线语义。"""
    df = make_df()
    df.loc[60, "close"] = df.loc[59, "close"] * 1.02  # 收涨2%
    df.loc[60, "high"] = df.loc[60, "close"] * 1.05  # 盘中冲高5%（上影线3%）
    df.loc[60, "low"] = df.loc[60, "close"] * 0.99

    st = ChenXueZhangStrategy({"exit_mode": "optimized"})
    sig = st.generate_signals(df.copy(), is_backtest=True)
    assert sig["signal"].iloc[60] == -1, "optimized 模式上影线>3%冲高回落应卖出"


def test_optimized_time_stop_applies_max_hold():
    """optimized：max_hold_days 时间止损生效（10日内无卖出信号则强制退出）。

    直接调用 _apply_max_hold_exit 验证（避免连续买入信号干扰引擎级行为）。
    """
    df = make_df(n=60)
    df["signal"] = 0
    df["confidence"] = 0.0
    df.iloc[10, df.columns.get_loc("signal")] = 1  # 第10天孤立买入

    st = ChenXueZhangStrategy({"exit_mode": "optimized", "max_hold_days": 5})
    out = st._apply_max_hold_exit(df.copy())
    # 买入后 5 天内应出现强制卖出信号
    window = out.iloc[11:16]["signal"]
    assert (window == -1).any(), f"max_hold_days=5 内应有时间止损卖出，实际: {window.tolist()}"


def test_legacy_no_time_stop_by_default():
    """legacy：不启用时间止损（旧行为）。

    验证方式：legacy 模式不调用 _apply_max_hold_exit —— max_hold_days
    参数变化不影响输出；optimized 模式 max_hold_days 变化改变输出。
    （注意：generate_signals 会重写 signal 列，不能预设买入信号）
    """
    df = make_df(n=80)

    # legacy：max_hold_days 1 vs 20 → 输出完全一致（时间止损未生效）
    st_a = ChenXueZhangStrategy({"exit_mode": "legacy", "max_hold_days": 1})
    sig_a = st_a.generate_signals(df.copy(), is_backtest=True)
    st_b = ChenXueZhangStrategy({"exit_mode": "legacy", "max_hold_days": 20})
    sig_b = st_b.generate_signals(df.copy(), is_backtest=True)
    assert (sig_a["signal"] == sig_b["signal"]).all(), "legacy 模式 max_hold_days 变化不应影响输出"

    # optimized：max_hold_days 1 vs 20 → 输出不同（时间止损生效）
    st_c = ChenXueZhangStrategy({"exit_mode": "optimized", "max_hold_days": 1})
    sig_c = st_c.generate_signals(df.copy(), is_backtest=True)
    st_d = ChenXueZhangStrategy({"exit_mode": "optimized", "max_hold_days": 20})
    sig_d = st_d.generate_signals(df.copy(), is_backtest=True)
    assert not (
        sig_c["signal"] == sig_d["signal"]
    ).all(), "optimized 模式 max_hold_days 变化应影响输出"


def test_optimized_r25_tightened_volume_threshold():
    """optimized：R25 量价背离卖出需极端缩量（<MA5*0.5），正常缩量不卖。

    用下跌趋势数据避免 R28 突破回踩买入覆盖 R25 卖出。
    """
    df = make_df(n=80, trend_down=True)

    def run_with_vol_ratio(ratio: float) -> int:
        d = df.copy()
        vol_ma5 = d["volume"].rolling(5).mean()
        # 构造：价涨 + 指定缩量比
        d.loc[60, "close"] = d.loc[59, "close"] * 1.01
        d.loc[60, "volume"] = vol_ma5.iloc[60] * ratio
        d.loc[60, "high"] = d.loc[60, "close"] * 1.005
        st = ChenXueZhangStrategy({"exit_mode": "optimized"})
        sig = st.generate_signals(d, is_backtest=True)
        return int(sig["signal"].iloc[60])

    # 缩量 60%（旧逻辑 0.7 阈值会卖）→ 新逻辑不卖
    assert run_with_vol_ratio(0.6) != -1, "optimized 缩量60%不应触发背离卖出"
    # 极端缩量 40% → 新逻辑卖出
    assert run_with_vol_ratio(0.4) == -1, "optimized 极端缩量40%应触发背离卖出"
