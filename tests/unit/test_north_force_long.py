"""NorthForceLongStrategy 单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.north_force_long import NorthForceLongStrategy


@pytest.fixture
def strategy() -> NorthForceLongStrategy:
    return NorthForceLongStrategy({})


def _make_df(
    prices: list[float],
    volumes: list[float],
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pd.DataFrame:
    df = pd.DataFrame({
        "date": pd.date_range(start="2023-01-01", periods=len(prices)),
        "open": opens if opens is not None else prices,
        "high": highs if highs is not None else [p * 1.01 for p in prices],
        "low": lows if lows is not None else [p * 0.99 for p in prices],
        "close": prices,
        "volume": volumes,
    })
    return df


def _build_uptrend_segment() -> pd.DataFrame:
    """构造一个能触发买入信号的 100 日上涨趋势段。"""
    prices = (
        list(np.linspace(10.0, 12.0, 55))
        + list(np.linspace(12.0, 12.6, 20))
        + list(np.linspace(12.6, 14.49, 25))
    )
    volumes = [10000] * 95 + [4000] * 5
    opens = [p * 0.998 for p in prices]
    df = _make_df(prices, volumes, opens)

    # 让最后一日收盘价贴近 MA20 上方，形成缩量回踩形态
    ma20 = df["close"].rolling(20).mean()
    df.loc[df.index[-1], "close"] = ma20.iloc[-1] * 1.005
    df.loc[df.index[-1], "open"] = df["close"].iloc[-1] * 0.998
    return df


def test_validate_params_bad_windows(strategy: NorthForceLongStrategy):
    """趋势窗必须小于长期窗。"""
    strategy.params = {"ma_trend_window": 60, "ma_long_window": 20}
    with pytest.raises(ValueError, match="必须小于"):
        strategy.validate_params()


def test_validate_params_pullback_must_be_positive(strategy: NorthForceLongStrategy):
    strategy.params = {"pullback_to_ma_pct": 0}
    with pytest.raises(ValueError, match="必须 > 0"):
        strategy.validate_params()


def test_validate_params_vol_ratio_floor(strategy: NorthForceLongStrategy):
    strategy.params = {"max_vol_ratio": 0.5}
    with pytest.raises(ValueError, match="必须 >= 1.0"):
        strategy.validate_params()


def test_no_signal_on_insufficient_data(strategy: NorthForceLongStrategy):
    """数据不足时应返回零信号。"""
    df = _make_df([10.0] * 5, [1000] * 5)
    result = strategy.generate_signals(df)
    assert result["signal"].sum() == 0
    assert result["confidence"].sum() == 0


def test_buy_signal_on_healthy_pullback(strategy: NorthForceLongStrategy):
    """趋势向上、缩量回踩 MA20、近期有资金关注时产生买入信号。"""
    df = _build_uptrend_segment()
    result = strategy.generate_signals(df)

    assert "signal" in result.columns
    assert "confidence" in result.columns
    assert result["signal"].iloc[-1] == 1
    assert result["confidence"].iloc[-1] >= 0.55


def test_sell_priority_over_buy(strategy: NorthForceLongStrategy):
    """同一日同时满足买卖条件时，卖出优先（时间止损覆盖买入）。"""
    df_buy = _build_uptrend_segment()
    entry_close = df_buy["close"].iloc[-1]

    # 买入后价格围绕成本小幅震荡，成交量保持高位（非买点）
    post = list(np.linspace(entry_close, entry_close * 1.02, 30))
    # 在观察窗内制造一个 5%+ 的阳线，满足 recent_spike 条件
    post[-5] = post[-6] * 1.055
    post[-4] = post[-5] * 1.002
    post[-3] = post[-4] * 1.001
    post[-2] = post[-3] * 1.001
    post[-1] = post[-2] * 1.001

    prices = list(df_buy["close"]) + post
    volumes = [10000] * 95 + [4000] * 5 + [20000] * 29 + [4000]
    opens = [p * 0.998 for p in prices]
    df = _make_df(prices, volumes, opens)

    ma20 = df["close"].rolling(20).mean()
    df.loc[df.index[-1], "close"] = ma20.iloc[-1] * 1.005
    df.loc[df.index[-1], "open"] = df["close"].iloc[-1] * 0.998

    result = strategy.generate_signals(df)

    # 存在买入信号，且最后一天因时间止损卖出优先于买入
    assert (result["signal"] == 1).any()
    assert result["signal"].iloc[-1] == -1
    assert result["days_since_entry"].iloc[-1] >= 30
    assert result["return_since_entry"].iloc[-1] < 0.05


def test_ma_death_cross_sells(strategy: NorthForceLongStrategy):
    """MA20 下穿 MA60 时产生卖出信号。"""
    df_buy = _build_uptrend_segment()
    entry_close = df_buy["close"].iloc[-1]

    # 买入后缓慢下跌，使 MA20 最终下穿 MA60
    post = [entry_close * (0.997 ** (i + 1)) for i in range(120)]
    prices = list(df_buy["close"]) + post
    volumes = [10000] * 95 + [4000] * 5 + [10000] * 120
    opens = [p * 0.998 for p in prices]
    df = _make_df(prices, volumes, opens)

    result = strategy.generate_signals(df)

    ma_trend = result["ma_trend"]
    ma_long = result["ma_long"]
    death_cross = (ma_trend < ma_long) & (ma_trend.shift(1) >= ma_long.shift(1))
    death_idx = result.index[death_cross]

    assert len(death_idx) > 0, "应出现 MA20 下穿 MA60"
    assert result.loc[death_idx[0], "signal"] == -1
