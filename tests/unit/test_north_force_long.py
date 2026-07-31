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
    df = pd.DataFrame(
        {
            "date": pd.date_range(start="2023-01-01", periods=len(prices)),
            "open": opens if opens is not None else prices,
            "high": highs if highs is not None else [p * 1.01 for p in prices],
            "low": lows if lows is not None else [p * 0.99 for p in prices],
            "close": prices,
            "volume": volumes,
        }
    )
    return df


def _build_uptrend_segment() -> pd.DataFrame:
    """构造一个能触发买入信号的 122 日上涨趋势段（覆盖默认 position_window）。"""
    prices = (
        list(np.linspace(10.0, 12.0, 77))
        + list(np.linspace(12.0, 12.6, 20))
        + list(np.linspace(12.6, 14.49, 25))
    )
    volumes = [10000] * 117 + [4000] * 5
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
    # 构造受控序列：先建立趋势并让均线走平，再 spike 制造近期关注，
    # 然后 entry → 横盘 → 最后一日同时触发买入+时间止损，卖出应优先。
    prices = list(np.linspace(10.0, 12.0, 40))
    prices += [12.0] * 60  # 均线走平，避免早期杂讯买入
    prices.append(12.0 * 1.06)  # index 100：制造 recent_spike
    prices += [12.0] * 2  # index 101-102：回踩，不产生买入
    prices.append(12.2)  # index 103：entry，收盘在 MA20 上方
    prices += [12.18] * 2  # index 104-105：横盘持有，收益<阈值
    prices.append(12.36)  # index 106：最后一日，仍满足买入+时间止损

    volumes = [10000] * 101 + [4000] * 6
    opens = [p * 0.998 for p in prices]
    df = _make_df(prices, volumes, opens)

    # 缩短时间止损窗口，使测试在可控长度内触发
    # min_confidence 0.30：构造数据的最高评分 0.389（103-105 日），
    # 原 0.40 阈值导致买入永不触发（测试意图是验证卖出优先，非评分门槛）
    test_strategy = NorthForceLongStrategy(
        {
            "min_confidence": 0.30,
            "time_stop_window": 3,
            "time_stop_threshold": 0.05,
        }
    )
    result = test_strategy.generate_signals(df)

    # 存在买入信号，且最后一天因时间止损卖出优先于买入
    assert (result["signal"] == 1).any()
    assert result["signal"].iloc[-1] == -1
    assert result["days_since_entry"].iloc[-1] >= 3
    assert result["return_since_entry"].iloc[-1] < 0.05


def test_ma_death_cross_sells(strategy: NorthForceLongStrategy):
    """MA20 下穿 MA60 前/时应触发卖出信号（状态机确保只卖一次）。"""
    df_buy = _build_uptrend_segment()
    entry_close = df_buy["close"].iloc[-1]

    # 买入后持续下跌，使 MA20 最终下穿 MA60
    post = [entry_close * (0.997 ** (i + 1)) for i in range(120)]
    prices = list(df_buy["close"]) + post
    volumes = [10000] * 117 + [4000] * 5 + [10000] * 120
    opens = [p * 0.998 for p in prices]
    df = _make_df(prices, volumes, opens)

    result = strategy.generate_signals(df)

    ma_trend = result["ma_trend"]
    ma_long = result["ma_long"]
    death_cross = (ma_trend < ma_long) & (ma_trend.shift(1) >= ma_long.shift(1))
    death_idx = result.index[death_cross]

    assert len(death_idx) > 0, "应出现 MA20 下穿 MA60"
    # 由于连续 2 日跌破 MA20 通常先于死亡交叉触发止损，
    # 因此断言在死亡交叉当天或之前存在卖出信号即可。
    first_death = death_idx[0]
    sells_before_or_at = (result.loc[:first_death, "signal"] == -1).any()
    assert sells_before_or_at, "MA20 下穿 MA60 时应已触发卖出"
