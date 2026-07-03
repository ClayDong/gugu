"""资金流跟随策略单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.money_flow_follow import MoneyFlowFollowStrategy
from gugu.strategies.registry import get_strategy


def _make_df(n: int = 150, seed: int = 42) -> pd.DataFrame:
    """生成测试用 OHLCV DataFrame。"""
    np.random.seed(seed)
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    prices = [10.0]
    for _ in range(1, n):
        prices.append(prices[-1] * (1 + np.random.uniform(0.001, 0.015)))
    prices = np.array(prices)
    volumes = np.random.uniform(1000, 2000, n).astype(float)
    return pd.DataFrame({
        "date": dates,
        "open": prices * 0.99,
        "high": prices * 1.01,
        "low": prices * 0.98,
        "close": prices,
        "volume": volumes,
        "amount": prices * volumes,
    })


def _make_bullish_pullback_df() -> pd.DataFrame:
    """构造一组精确满足5步条件的看涨数据。

    1. 近期强势涨幅 ≥ 3%
    2. 均线多头排列 MA5 > MA10 > MA20 > MA60
    3. 非历史天量
    4. 创新高回踩不破 MA20
    5. 缩量回踩后放量确认
    """
    np.random.seed(123)
    n = 150
    dates = pd.date_range("2026-01-01", periods=n, freq="B")

    # 上涨趋势直到 day 140
    prices = [10.0]
    for _ in range(1, 141):
        prices.append(prices[-1] * (1 + np.random.uniform(0.008, 0.02)))
    # 缩量回踩 day 141-143
    for _ in range(141, 144):
        prices.append(prices[-1] * (1 - np.random.uniform(0.001, 0.005)))
    # 放量确认 day 144-150
    for _ in range(144, n):
        prices.append(prices[-1] * (1 + np.random.uniform(0.003, 0.01)))
    prices = np.array(prices)

    volumes = np.random.uniform(1000, 2000, n).astype(float)
    # day 142-143 缩量递减
    volumes[142] = 800
    volumes[143] = 400
    # day 144 放量确认 (900/400 = 2.25 >= 1.5)
    volumes[144] = 900

    return pd.DataFrame({
        "date": dates,
        "open": prices * 0.99,
        "high": prices * 1.02,
        "low": prices * 0.98,
        "close": prices,
        "volume": volumes,
        "amount": prices * volumes,
    })


class TestMoneyFlowFollowRegistration:
    """策略注册测试。"""

    def test_strategy_registered_in_registry(self) -> None:
        s = get_strategy("money_flow_follow")
        assert s.name == "money_flow_follow"
        assert isinstance(s, MoneyFlowFollowStrategy)

    def test_strategy_has_default_params(self) -> None:
        s = get_strategy("money_flow_follow")
        assert s.params["ma_periods"] == [5, 10, 20, 60]
        assert s.params["new_high_window"] == 20
        assert s.params["volume_expand_ratio"] == 1.5
        assert s.params["abnormal_volume_ratio"] == 3.0
        assert s.params["recent_gain_threshold"] == 0.03


class TestMoneyFlowFollowSignalGeneration:
    """信号生成逻辑测试。"""

    def test_generates_buy_signal_on_bullish_pullback(self) -> None:
        """满足5步条件时应产生买入信号。"""
        df = _make_bullish_pullback_df()
        s = MoneyFlowFollowStrategy()
        result = s.generate_signals(df)

        buy_signals = result[result["signal"] == 1]
        assert len(buy_signals) >= 1, "应至少产生1个买入信号"
        # 确认 day 144 附近有买入信号
        signal_dates = buy_signals["date"].dt.strftime("%Y-%m-%d").tolist()
        assert any("2026-07" in d for d in signal_dates), f"买入信号应出现在放量确认日, got {signal_dates}"

    def test_no_buy_signal_on_flat_market(self) -> None:
        """震荡市（无趋势）不应产生买入信号。"""
        np.random.seed(42)
        n = 150
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        # 价格在 10±0.5 震荡
        prices = 10.0 + np.random.uniform(-0.5, 0.5, n)
        volumes = np.random.uniform(1000, 2000, n).astype(float)
        df = pd.DataFrame({
            "date": dates, "open": prices * 0.99, "high": prices * 1.01,
            "low": prices * 0.98, "close": prices, "volume": volumes,
            "amount": prices * volumes,
        })
        s = MoneyFlowFollowStrategy()
        result = s.generate_signals(df)
        # 震荡市不应有买入信号（均线多头排列不成立）
        assert (result["signal"] == 1).sum() == 0

    def test_sell_signal_on_ma_breakdown(self) -> None:
        """MA5 下穿 MA10 时应产生卖出信号。"""
        np.random.seed(42)
        n = 150
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        # 先涨后跌
        prices = [10.0]
        for _ in range(1, 100):
            prices.append(prices[-1] * 1.01)
        for _ in range(100, n):
            prices.append(prices[-1] * 0.98)
        prices = np.array(prices)
        volumes = np.random.uniform(1000, 2000, n).astype(float)
        df = pd.DataFrame({
            "date": dates, "open": prices * 0.99, "high": prices * 1.01,
            "low": prices * 0.98, "close": prices, "volume": volumes,
            "amount": prices * volumes,
        })
        s = MoneyFlowFollowStrategy()
        result = s.generate_signals(df)
        # 下跌阶段应有卖出信号
        assert (result["signal"] == -1).sum() > 0

    def test_confidence_in_range(self) -> None:
        """置信度应在 [0, 1] 范围内。"""
        df = _make_df()
        s = MoneyFlowFollowStrategy()
        result = s.generate_signals(df)
        assert result["confidence"].min() >= 0.0
        assert result["confidence"].max() <= 1.0

    def test_no_signal_when_zero_confidence(self) -> None:
        """signal=0 时 confidence 应为 0。"""
        df = _make_df()
        s = MoneyFlowFollowStrategy()
        result = s.generate_signals(df)
        zero_signal_rows = result[result["signal"] == 0]
        if not zero_signal_rows.empty:
            assert (zero_signal_rows["confidence"] == 0.0).all()

    def test_sell_confidence_values(self) -> None:
        """卖出信号置信度应为 0.6 或 0.8。"""
        np.random.seed(42)
        n = 150
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        prices = [10.0]
        for _ in range(1, 100):
            prices.append(prices[-1] * 1.01)
        for _ in range(100, n):
            prices.append(prices[-1] * 0.95)
        prices = np.array(prices)
        volumes = np.random.uniform(1000, 2000, n).astype(float)
        df = pd.DataFrame({
            "date": dates, "open": prices * 0.99, "high": prices * 1.01,
            "low": prices * 0.98, "close": prices, "volume": volumes,
            "amount": prices * volumes,
        })
        s = MoneyFlowFollowStrategy()
        result = s.generate_signals(df)
        sell_rows = result[result["signal"] == -1]
        if not sell_rows.empty:
            conf_values = sell_rows["confidence"].unique()
            assert all(c in (0.6, 0.8) for c in conf_values), f"卖出置信度应为 0.6 或 0.8, got {conf_values}"


class TestMoneyFlowFollowConditions:
    """5步过滤条件单元测试。"""

    def test_abnormal_volume_excluded(self) -> None:
        """历史天量应被剔除（不产生买入信号）。"""
        df = _make_bullish_pullback_df()
        # 在放量确认日制造一个天量（>3倍60日均量）
        vol_ma = df["volume"].rolling(60).mean()
        df.loc[144, "volume"] = vol_ma.iloc[144] * 5.0
        s = MoneyFlowFollowStrategy()
        result = s.generate_signals(df)
        # day 144 不应有买入信号（天量被剔除）
        assert result.loc[144, "signal"] != 1

    def test_volume_expansion_required(self) -> None:
        """无放量确认时不应产生买入信号。"""
        df = _make_bullish_pullback_df()
        # 取消放量（day 144 成交量设为和 day 143 相同）
        df.loc[144, "volume"] = df.loc[143, "volume"]
        s = MoneyFlowFollowStrategy()
        result = s.generate_signals(df)
        # day 144 不应有买入信号（无放量确认）
        assert result.loc[144, "signal"] != 1

    def test_ma_alignment_required(self) -> None:
        """均线非多头排列时不应产生买入信号。"""
        df = _make_bullish_pullback_df()
        # 破坏均线排列：在最后几天下跌使 MA5 < MA10
        for i in range(140, 150):
            df.loc[i, "close"] = df.loc[i, "close"] * 0.95
            df.loc[i, "high"] = df.loc[i, "close"] * 1.01
            df.loc[i, "low"] = df.loc[i, "close"] * 0.99
        s = MoneyFlowFollowStrategy()
        result = s.generate_signals(df)
        # 最后几天不应有买入信号（均线排列被破坏）
        last_10 = result.tail(10)
        assert (last_10["signal"] == 1).sum() == 0

    def test_validate_params_rejects_bad_ma_periods(self) -> None:
        """ma_periods 长度不足4时应抛出异常。"""
        with pytest.raises(ValueError, match="ma_periods"):
            MoneyFlowFollowStrategy(params={"ma_periods": [5, 10]})

    def test_custom_params_override_defaults(self) -> None:
        """自定义参数应覆盖默认值。"""
        s = MoneyFlowFollowStrategy(params={"volume_expand_ratio": 2.0})
        assert s.params["volume_expand_ratio"] == 2.0
        # 其他参数仍为默认
        assert s.params["ma_periods"] == [5, 10, 20, 60]
