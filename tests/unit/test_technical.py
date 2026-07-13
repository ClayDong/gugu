"""技术指标工具函数单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.analysis.technical import (
    atr,
    bollinger,
    ema,
    macd,
    obv,
    rsi,
    sma,
)


def _make_df(prices: list[float]) -> pd.DataFrame:
    """从收盘价序列构造 DataFrame（模拟 high=close*1.02, low=close*0.98）。"""
    data = {
        "open": [p * 0.99 for p in prices],
        "high": [p * 1.02 for p in prices],
        "low": [p * 0.98 for p in prices],
        "close": prices,
        "volume": [100000] * len(prices),
    }
    return pd.DataFrame(data)


class TestATR:
    """ATR 计算测试。"""

    def test_atr_basic(self):
        """基本 ATR 计算：稳定行情 ATR 反映高-低波动。"""
        # high=close*1.02, low=close*0.98 → TR=2% of close
        df = _make_df([100.0] * 20)
        result = atr(df, period=14)
        assert not result.empty
        # 稳定行情下 ATR = 4.0（H-L spread = 4% of 100）
        assert result.iloc[-1] == 4.0

    def test_atr_volatile(self):
        """剧烈波动行情 ATR 应较大。"""
        # 连续大幅波动
        prices = [100.0]
        for i in range(19):
            if i % 2 == 0:
                prices.append(prices[-1] * 1.05)  # 涨5%
            else:
                prices.append(prices[-1] * 0.95)  # 跌5%
        df = _make_df(prices)
        result = atr(df, period=14)
        assert not result.empty
        assert result.iloc[-1] > 0

    def test_atr_returns_series(self):
        """ATR 返回值类型应为 pd.Series。"""
        df = _make_df([float(i) for i in range(20)])
        result = atr(df, period=5)
        assert isinstance(result, pd.Series)
        assert len(result) == 20

    def test_atr_period_14_default(self):
        """默认 period=14。"""
        from inspect import signature

        sig = signature(atr)
        assert sig.parameters["period"].default == 14


class TestSMA:
    """简单移动平均测试。"""

    def test_sma_basic(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = sma(s, 3)
        assert result.iloc[2] == 2.0  # (1+2+3)/3
        assert result.iloc[4] == 4.0  # (3+4+5)/3

    def test_sma_short_window(self):
        s = pd.Series([10.0, 20.0])
        result = sma(s, 5)
        # min_periods=1，短序列也有结果
        assert result.iloc[-1] == 15.0


class TestEMA:
    """指数移动平均测试。"""

    def test_ema_trend(self):
        """上升序列 EMA 应低于最新价（滞后性）。"""
        s = pd.Series([float(i) for i in range(1, 21)])
        result = ema(s, 10)
        assert result.iloc[-1] < 20.0
        assert result.iloc[-1] > 10.0


class TestRSI:
    """RSI 计算测试。"""

    def test_rsi_basic(self):
        """基本 RSI 计算。"""
        s = pd.Series([float(i) for i in range(1, 21)])  # 持续上升
        result = rsi(s, period=14)
        assert not result.empty
        assert result.iloc[-1] >= 50  # 上升趋势 RSI >= 50

    def test_rsi_overbought(self):
        """持续大涨后 RSI 应进入超买区。"""
        s = pd.Series([100.0 * (1.02**i) for i in range(30)])  # 持续大涨
        result = rsi(s, period=14)
        assert result.iloc[-1] > 70

    def test_rsi_oversold(self):
        """持续大跌后 RSI 应进入超卖区。"""
        s = pd.Series([100.0 * (0.98**i) for i in range(30)])  # 持续大跌
        result = rsi(s, period=14)
        assert result.iloc[-1] < 30

    def test_rsi_range(self):
        """RSI 值应在 0-100 范围内。"""
        s = pd.Series(np.random.randn(30) * 5 + 100)
        result = rsi(s, period=14)
        assert result.iloc[-1] >= 0
        assert result.iloc[-1] <= 100

    def test_rsi_returns_series(self):
        """RSI 返回 pd.Series。"""
        s = pd.Series([float(i) for i in range(30)])
        result = rsi(s, period=14)
        assert isinstance(result, pd.Series)


class TestMACD:
    """MACD 计算测试。"""

    def test_macd_basic(self):
        """基本 MACD 计算。"""
        s = pd.Series([float(i) for i in range(1, 41)])
        dif, dea, hist = macd(s)
        assert not dif.empty
        assert not dea.empty
        assert not hist.empty

    def test_macd_uptrend(self):
        """上升趋势中 DIF > DEA。"""
        s = pd.Series([100.0 + i * 0.5 for i in range(60)])
        dif, dea, hist = macd(s)
        assert dif.iloc[-1] > dea.iloc[-1]
        assert hist.iloc[-1] > 0

    def test_macd_downtrend(self):
        """下降趋势中 DIF < DEA。"""
        s = pd.Series([100.0 - i * 0.5 for i in range(60)])
        dif, dea, hist = macd(s)
        assert dif.iloc[-1] < dea.iloc[-1]
        assert hist.iloc[-1] < 0

    def test_macd_returns_tuple(self):
        """MACD 返回三元组。"""
        s = pd.Series([float(i) for i in range(40)])
        result = macd(s)
        assert len(result) == 3


class TestBollinger:
    """布林带计算测试。"""

    def test_bollinger_basic(self):
        """基本布林带计算。"""
        s = pd.Series([100.0] * 30)
        middle, upper, lower = bollinger(s)
        assert middle.iloc[-1] == 100.0
        assert upper.iloc[-1] >= middle.iloc[-1]
        assert lower.iloc[-1] <= middle.iloc[-1]

    def test_bollinger_volatile(self):
        """波动行情布林带应更宽。"""
        s = pd.Series([100.0 + np.random.randn() * 10 for _ in range(30)])
        middle, upper, lower = bollinger(s)
        assert upper.iloc[-1] > lower.iloc[-1]

    def test_bollinger_price_within_bands(self):
        """价格应在布林带范围内。"""
        s = pd.Series([100.0 + i * 0.3 for i in range(30)])
        middle, upper, lower = bollinger(s)
        assert lower.iloc[-1] <= s.iloc[-1] <= upper.iloc[-1]


class TestOBV:
    """OBV 计算测试。"""

    def test_obv_basic(self):
        """基本 OBV 计算。"""
        df = _make_df([100.0] * 20)
        result = obv(df)
        assert not result.empty
        assert isinstance(result, pd.Series)

    def test_obv_uptrend(self):
        """上涨时 OBV 应上升。"""
        prices = [100.0 * (1.01**i) for i in range(20)]
        df = _make_df(prices)
        result = obv(df)
        assert result.iloc[-1] > result.iloc[0]

    def test_obv_downtrend(self):
        """下跌时 OBV 应下降。"""
        prices = [100.0 * (0.99**i) for i in range(20)]
        df = _make_df(prices)
        result = obv(df)
        assert result.iloc[-1] < result.iloc[0]
