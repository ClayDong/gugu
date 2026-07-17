"""DragonTigerShortStrategy 单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.dragon_tiger_short import DragonTigerShortStrategy


@pytest.fixture
def strategy() -> DragonTigerShortStrategy:
    return DragonTigerShortStrategy({})


def _make_df(prices: list[float], volumes: list[float]) -> pd.DataFrame:
    df = pd.DataFrame({
        "date": pd.date_range(end="2026-07-17", periods=len(prices)),
        "open": [p * 0.99 for p in prices],
        "high": [p * 1.02 for p in prices],
        "low": [p * 0.98 for p in prices],
        "close": prices,
        "volume": volumes,
    })
    return df


def test_validate_params_bad_lookback(strategy: DragonTigerShortStrategy):
    strategy.params = {"limit_up_lookback": 0}
    with pytest.raises(ValueError, match="必须大于"):
        strategy.validate_params()


def test_validate_params_bad_limit_up(strategy: DragonTigerShortStrategy):
    strategy.params = {"limit_up_pct": 0.15}
    with pytest.raises(ValueError, match="5%-11%"):
        strategy.validate_params()


def test_validate_params_bad_max_vol_ratio(strategy: DragonTigerShortStrategy):
    strategy.params = {"max_vol_ratio": 0.5}
    with pytest.raises(ValueError, match="必须 >= 1.0"):
        strategy.validate_params()


def test_insufficient_data(strategy: DragonTigerShortStrategy):
    df = _make_df([10.0, 10.5, 11.0, 10.8, 11.2], [10000] * 5)
    result = strategy.generate_signals(df)
    assert "signal" in result.columns
    assert "confidence" in result.columns
    assert (result["signal"] == 0).all()
    assert (result["confidence"] == 0.0).all()


def test_buy_after_limit_up_break_rebound(strategy: DragonTigerShortStrategy):
    # 构造 37 日数据：第 34 日涨停，第 35 日断板（缩量），第 36 日阳线反包
    prices = list(np.linspace(10, 12, 34))
    prices.append(12 * 1.095)          # 34: 涨停
    prices.append(12 * 1.095 * 0.98)   # 35: 断板
    prices.append(13.50)               # 36: 反包（收盘价高于涨停日最高价）
    volumes = [10000] * 34 + [15000, 5000, 12000]
    df = _make_df(prices, volumes)
    result = strategy.generate_signals(df)
    assert "signal" in result.columns
    assert "confidence" in result.columns
    assert result["signal"].iloc[36] == 1
    assert result["confidence"].iloc[36] >= 0.55


def test_sell_on_stop_loss(strategy: DragonTigerShortStrategy):
    # 构造 38 日数据：第 36 日反包买入，第 37 日大跌触发止损
    prices = list(np.linspace(10, 12, 34))
    prices.append(12 * 1.095)          # 34: 涨停
    prices.append(12 * 1.095 * 0.98)   # 35: 断板
    prices.append(13.50)               # 36: 反包买入
    prices.append(12.0)                # 37: 跌破入场 low 的 95%，触发止损
    volumes = [10000] * 34 + [15000, 5000, 12000, 10000]
    df = _make_df(prices, volumes)
    result = strategy.generate_signals(df)
    assert result["signal"].iloc[36] == 1
    assert result["signal"].iloc[37] == -1
