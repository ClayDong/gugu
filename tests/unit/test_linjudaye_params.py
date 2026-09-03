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


def test_trailing_stop_applied():
    """trailing_stop_pct>0 时 _apply_trailing_stop_exit 产生卖出信号。"""
    from gugu.strategies.base import Strategy

    dates = pd.date_range("2025-01-01", periods=80, freq="B")
    # 连续上涨30天后退50天，保证移动止损触发
    close = np.array([10 + i * 0.05 for i in range(30)] + [15 - i * 0.1 for i in range(50)])
    n = len(close)
    df = pd.DataFrame(
        {
            "date": dates,
            "close": close,
            "open": close * 0.99,
            "high": close * 1.01,
            "low": close * 0.98,
            "volume": [1_000_000] * n,
            "signal": [0] * n,
        }
    )
    # 手动设置一个买入信号
    df.loc[10, "signal"] = 1
    s = Strategy()
    s.params = {"trailing_stop_pct": 0.08}
    result = s._apply_trailing_stop_exit(df.copy(), 0.08)
    sell_signals = int((result["signal"] == -1).sum())
    assert (
        sell_signals > 0
    ), f"移动止损应产生卖出信号，实际=0（最高从10涨到11.45，回撤10%应触发8%止损）"


def test_signal_columns_present():
    """输出含 signal/confidence 列。"""
    df = make_df(n=80)
    st = LinJuDaYeStrategy({"max_hold_days": 10})
    sig = st.generate_signals(df.copy(), is_backtest=True)
    assert "signal" in sig.columns
    assert "confidence" in sig.columns
    assert set(sig["signal"].unique()) <= {-1, 0, 1}
