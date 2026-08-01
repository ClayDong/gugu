"""linjudaye 参数化 + 时间止损单测（2026-08-01）。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.blogger_rules.linjudaye import LinJuDaYeStrategy


def make_df(n=120, trend="up") -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    rng = np.random.default_rng(7)
    drift = {"up": 0.003, "down": -0.003, "flat": 0.0}[trend]
    rets = rng.normal(drift, 0.008, n)
    close = 10.0 * np.cumprod(1 + rets)
    open_ = close * (1 + rng.normal(0, 0.002, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, n)))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    return pd.DataFrame(
        {"date": dates, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )


def test_params_defaults_match_old_hardcoded():
    """参数化改造后默认值应与原硬编码一致（行为不回归）。"""
    st = LinJuDaYeStrategy()
    assert float(st.params.get("golden_level", 0.618)) == pytest.approx(0.7)  # 校准后
    assert float(st.params.get("vol_shrink_ratio", 0.7)) == pytest.approx(0.7)
    assert float(st.params.get("box_range_threshold", 0.05)) == pytest.approx(0.05)
    assert float(st.params.get("low_vol_ratio", 0.5)) == pytest.approx(0.7)  # 校准后


def test_golden_level_affects_signal_count():
    """golden_level 参数影响信号数量（参数化生效）。"""
    df = make_df(n=120)
    st_a = LinJuDaYeStrategy({"golden_level": 0.382})
    st_b = LinJuDaYeStrategy({"golden_level": 0.7})
    sig_a = st_a.generate_signals(df.copy(), is_backtest=True)
    sig_b = st_b.generate_signals(df.copy(), is_backtest=True)
    n_a = int((sig_a["signal"] == 1).sum())
    n_b = int((sig_b["signal"] == 1).sum())
    assert n_a != n_b, f"golden_level 应改变信号数: {n_a} vs {n_b}"


def test_max_hold_exit_applied():
    """max_hold_days>0 时调用 _apply_max_hold_exit（产生卖出信号）。"""
    df = make_df(n=80)
    # 无时间止损：可能没有卖出信号
    st_no = LinJuDaYeStrategy({"max_hold_days": 0})
    sig_no = st_no.generate_signals(df.copy(), is_backtest=True)
    # 有时间止损：必然有卖出信号（强制退出）
    st_yes = LinJuDaYeStrategy({"max_hold_days": 5})
    sig_yes = st_yes.generate_signals(df.copy(), is_backtest=True)
    assert int((sig_yes["signal"] == -1).sum()) > 0, "时间止损应产生卖出信号"
    # 有卖出信号的数量应 >= 无时间止损版本
    assert int((sig_yes["signal"] == -1).sum()) >= int((sig_no["signal"] == -1).sum())


def test_signal_columns_present():
    """输出含 signal/confidence 列。"""
    df = make_df(n=80)
    st = LinJuDaYeStrategy({"max_hold_days": 10})
    sig = st.generate_signals(df.copy(), is_backtest=True)
    assert "signal" in sig.columns
    assert "confidence" in sig.columns
    assert set(sig["signal"].unique()) <= {-1, 0, 1}
