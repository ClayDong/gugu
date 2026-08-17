"""MA5MomentumStrategy 单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.ma5_momentum import MA5MomentumStrategy


def _make_df(
    close: list[float],
    volume: list[int],
    dates: list[pd.Timestamp] | None = None,
) -> pd.DataFrame:
    n = len(close)
    if dates is None:
        dates = pd.bdate_range("2024-01-01", periods=n)
    close_arr = np.asarray(close, dtype=float)
    rng = np.random.default_rng(42)
    high = close_arr + rng.uniform(0, 1, n)
    low = close_arr - rng.uniform(0, 1, n)
    open_ = low + rng.uniform(0, 1, n) * (high - low)
    return pd.DataFrame(
        {
            "date": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close_arr,
            "volume": np.asarray(volume, dtype=int),
            "amount": close_arr * np.asarray(volume, dtype=int),
        }
    )


def test_ma5_momentum_generates_signal_and_confidence() -> None:
    """策略应输出 signal 和 confidence 列。"""
    df = _make_df([100.0] * 30, [1_000_000] * 30)
    strat = MA5MomentumStrategy()
    out = strat.generate_signals(df)
    assert "signal" in out.columns
    assert "confidence" in out.columns
    assert set(out["signal"].unique()).issubset({-1, 0, 1})


def test_ma5_momentum_buy_after_big_yang_and_pullback() -> None:
    """放量大阳线后回踩 MA5 应产生买入信号。"""
    n = 40
    dates = pd.bdate_range("2024-01-01", periods=n)
    # 前 10 天缓慢上涨，让 MA5 有基础
    close = [100.0 + i * 0.1 for i in range(10)]
    volume = [500_000] * 10
    # 第 11 天放量大阳线（涨幅 >5%，量 3 倍）
    close.append(106.0)
    volume.append(1_500_000)
    # 后续几天：价格横盘/小幅回踩，收盘价仍在 MA5 上方，且缩量
    for _ in range(11, n):
        close.append(106.0)
        volume.append(300_000)

    df = _make_df(close, volume, dates)
    strat = MA5MomentumStrategy()
    out = strat.generate_signals(df)
    assert (out["signal"] == 1).any(), "应产生买入信号"


def test_ma5_momentum_excludes_current_day_from_recent_big_yang() -> None:
    close = [100.0] * 10 + [106.0] + [106.0] * 4
    volume = [500_000] * 10 + [1_500_000] + [300_000] * 4
    out = MA5MomentumStrategy({"big_yang_lookback": 3})._generate_signals_impl(
        _make_df(close, volume)
    )
    assert bool(out.loc[10, "recent_big_yang"]) is False


def test_ma5_momentum_rejects_invalid_windows() -> None:
    with pytest.raises(ValueError, match="big_yang_lookback"):
        MA5MomentumStrategy({"big_yang_lookback": 0})
    with pytest.raises(ValueError, match="vol_ma_window"):
        MA5MomentumStrategy({"vol_ma_window": 0})


def test_ma5_momentum_no_buy_without_big_yang() -> None:
    """没有放量大阳线时不应产生买入信号。"""
    n = 30
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = [100.0 + i * 0.2 for i in range(n)]
    volume = [1_000_000] * n
    df = _make_df(close, volume, dates)
    strat = MA5MomentumStrategy()
    out = strat.generate_signals(df)
    assert not (out["signal"] == 1).any(), "没有放量大阳线时不应买入"


def test_ma5_momentum_sell_on_deviation() -> None:
    """股价明显偏离 MA5 时应产生卖出信号。"""
    n = 30
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = [100.0 + i * 0.1 for i in range(10)]
    volume = [1_000_000] * 10
    # 放量大阳线
    close.append(105.0)
    volume.append(2_000_000)
    # 后续连续大涨，偏离 MA5 超过 5%
    for _ in range(11, n):
        close.append(close[-1] * 1.06)
        volume.append(1_000_000)

    df = _make_df(close, volume, dates)
    strat = MA5MomentumStrategy()
    out = strat.generate_signals(df)
    assert (out["signal"] == -1).any(), "偏离 MA5 应产生卖出信号"


def test_ma5_momentum_force_exit_after_three_days_below_ma5() -> None:
    """跌破 MA5 连续 3 日应强制清仓。"""
    n = 30
    dates = pd.bdate_range("2024-01-01", periods=n)
    close: list[float] = [100.0 + i * 0.1 for i in range(10)]
    volume: list[int] = [1_000_000] * 10
    # 放量大阳线
    close.append(105.0)
    volume.append(2_000_000)
    # 先涨几天
    for _ in range(5):
        close.append(close[-1] + 0.5)
        volume.append(1_000_000)
    # 连续大跌，跌破 MA5 且 3 日站不回
    for _ in range(5):
        close.append(close[-1] * 0.95)
        volume.append(1_000_000)

    # 补齐到 n 天
    while len(close) < n:
        close.append(close[-1] * 0.95)
        volume.append(1_000_000)

    df = _make_df(close, volume, dates)
    strat = MA5MomentumStrategy()
    out = strat.generate_signals(df)
    assert (out["signal"] == -1).any(), "连续跌破 MA5 应产生卖出信号"


def test_ma5_momentum_registry() -> None:
    """策略应正确注册到注册表。"""
    from gugu.strategies.registry import get_strategy, list_strategies

    assert "ma5_momentum" in list_strategies()
    strat = get_strategy("ma5_momentum")
    assert isinstance(strat, MA5MomentumStrategy)
