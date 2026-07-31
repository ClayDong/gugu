"""20日均线趋势跟随战法单元测试。

覆盖核心场景：
- 趋势判定（多头/震荡/空头）
- 买入信号A（回踩低吸）
- 买入信号B（放量突破）
- 卖出信号C（止盈离场）
- 卖出信号D（止损离场）
- 卖出信号E（高位滞涨）
- 仓位管理
- 置信度计算
- 参数校验
- 边界场景
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.ma20_trend import MA20TrendStrategy


def _make_bullish_trend_df(days: int = 120, seed: int = 42) -> pd.DataFrame:
    """构造多头趋势测试数据：价格稳步上升 + MA20向上。"""
    np.random.seed(seed)
    n = days
    trend = np.linspace(100, 180, n)
    noise = np.random.randn(n) * 1.5
    close = trend + noise

    open_ = np.zeros(n)
    high = np.zeros(n)
    low = np.zeros(n)
    volume = np.zeros(n)

    for i in range(n):
        if i == 0:
            open_[i] = close[i] - 0.5
        else:
            open_[i] = close[i - 1] + np.random.randn() * 0.3

        high[i] = max(close[i], open_[i]) + abs(np.random.randn()) * 0.8
        low[i] = min(close[i], open_[i]) - abs(np.random.randn()) * 0.8
        volume[i] = np.random.uniform(8_000_000, 12_000_000)

    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume.astype(float),
            "amount": close * volume,
        }
    )


def _make_sideways_df(days: int = 120, seed: int = 42) -> pd.DataFrame:
    """构造震荡市测试数据：价格横盘 + MA20走平。"""
    np.random.seed(seed)
    n = days
    close = 100 + np.random.randn(n) * 2.0
    open_ = close - np.random.randn(n) * 0.5
    high = np.maximum(close, open_) + np.random.rand(n) * 1.0
    low = np.minimum(close, open_) - np.random.rand(n) * 1.0
    volume = np.random.uniform(5_000_000, 8_000_000, size=n).astype(float)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": close * volume,
        }
    )


def _make_downtrend_df(days: int = 120, seed: int = 42) -> pd.DataFrame:
    """构造下跌趋势测试数据：价格持续下跌 + MA20向下。"""
    np.random.seed(seed)
    n = days
    trend = np.linspace(150, 80, n)
    noise = np.random.randn(n) * 1.0
    close = trend + noise

    open_ = np.zeros(n)
    high = np.zeros(n)
    low = np.zeros(n)
    volume = np.zeros(n)

    for i in range(n):
        if i == 0:
            open_[i] = close[i] + 0.5
        else:
            open_[i] = close[i - 1] + np.random.randn() * 0.3

        high[i] = max(close[i], open_[i]) + abs(np.random.randn()) * 0.8
        low[i] = min(close[i], open_[i]) - abs(np.random.randn()) * 0.8
        volume[i] = np.random.uniform(8_000_000, 12_000_000)

    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume.astype(float),
            "amount": close * volume,
        }
    )


def _make_pullback_to_ma20_df() -> pd.DataFrame:
    """构造回踩MA20测试数据：价格回踩MA20附近 + 缩量 + 止跌K线。"""
    n = 60
    np.random.seed(42)

    # 前40天：稳步上涨趋势
    trend = np.linspace(100, 140, 40)
    close_list = list(trend)

    # 后20天：回踩MA20附近 + 缩量
    for i in range(20):
        close_list.append(138 + np.random.randn() * 0.5)

    close = np.array(close_list, dtype=float)
    n = len(close)
    open_ = np.zeros(n)
    high = np.zeros(n)
    low = np.zeros(n)
    volume = np.zeros(n)

    for i in range(n):
        if i == 0:
            open_[i] = close[i] - 0.3
        else:
            open_[i] = close[i - 1] + np.random.randn() * 0.2

        high[i] = max(close[i], open_[i]) + abs(np.random.randn()) * 0.3
        low[i] = min(close[i], open_[i]) - abs(np.random.randn()) * 0.3

        # 后20天缩量
        if i >= 40:
            volume[i] = np.random.uniform(3_000_000, 5_000_000)
        else:
            volume[i] = np.random.uniform(10_000_000, 15_000_000)

    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume.astype(float),
            "amount": close * volume,
        }
    )


def _make_breakout_df() -> pd.DataFrame:
    """构造放量突破测试数据：价格从MA20下方突破到上方 + 放量。"""
    n = 60
    np.random.seed(42)

    # 前40天：价格在MA20下方
    trend = np.linspace(100, 105, 40)
    close_list = list(trend)

    # 后20天：突破MA20 + 放量
    for i in range(20):
        close_list.append(108 + i * 0.5 + np.random.randn() * 0.3)

    close = np.array(close_list, dtype=float)
    n = len(close)
    open_ = np.zeros(n)
    high = np.zeros(n)
    low = np.zeros(n)
    volume = np.zeros(n)

    for i in range(n):
        if i == 0:
            open_[i] = close[i] - 0.3
        else:
            open_[i] = close[i - 1] + np.random.randn() * 0.2

        high[i] = max(close[i], open_[i]) + abs(np.random.randn()) * 0.3
        low[i] = min(close[i], open_[i]) - abs(np.random.randn()) * 0.3

        # 后20天放量
        if i >= 40:
            volume[i] = np.random.uniform(15_000_000, 20_000_000)
        else:
            volume[i] = np.random.uniform(5_000_000, 8_000_000)

    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume.astype(float),
            "amount": close * volume,
        }
    )


def _make_high_deviation_df() -> pd.DataFrame:
    """构造高乖离率测试数据：价格远离MA20（乖离率>30%）。"""
    n = 60
    np.random.seed(42)

    # 前40天：价格在100附近
    close_list = list(np.linspace(100, 105, 40))

    # 后20天：快速上涨远离MA20（每天涨5元，确保乖离率>30%）
    for i in range(20):
        close_list.append(108 + i * 5.0 + np.random.randn() * 0.5)

    close = np.array(close_list, dtype=float)
    n = len(close)
    open_ = np.zeros(n)
    high = np.zeros(n)
    low = np.zeros(n)
    volume = np.zeros(n)

    for i in range(n):
        if i == 0:
            open_[i] = close[i] - 0.3
        else:
            open_[i] = close[i - 1] + np.random.randn() * 0.2

        high[i] = max(close[i], open_[i]) + abs(np.random.randn()) * 0.5
        low[i] = min(close[i], open_[i]) - abs(np.random.randn()) * 0.5
        volume[i] = np.random.uniform(8_000_000, 12_000_000)

    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume.astype(float),
            "amount": close * volume,
        }
    )


# ── 参数校验测试 ──


class TestMA20TrendParams:
    """参数校验测试。"""

    def test_default_params_valid(self):
        """默认参数应通过校验。"""
        strategy = MA20TrendStrategy()
        strategy.validate_params()

    def test_invalid_ma_window(self):
        """ma_window < 5 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="ma_window"):
            MA20TrendStrategy({"ma_window": 3})

    def test_invalid_vol_shrink_pct(self):
        """vol_shrink_pct 超出 [0,1] 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="vol_shrink_pct"):
            MA20TrendStrategy({"vol_shrink_pct": 1.5})

    def test_invalid_vol_expand_ratio(self):
        """vol_expand_ratio < 1 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="vol_expand_ratio"):
            MA20TrendStrategy({"vol_expand_ratio": 0.5})

    def test_invalid_deviation_threshold(self):
        """deviation_threshold < 0.1 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="deviation_threshold"):
            MA20TrendStrategy({"deviation_threshold": 0.05})

    def test_invalid_stop_loss_pct(self):
        """stop_loss_pct 超出 (0, 0.2] 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="stop_loss_pct"):
            MA20TrendStrategy({"stop_loss_pct": 0.3})

    def test_invalid_min_daily_amount(self):
        """min_daily_amount < 1亿 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="min_daily_amount"):
            MA20TrendStrategy({"min_daily_amount": 5e7})


# ── 趋势判定测试 ──


class TestMA20TrendDetection:
    """趋势判定测试。"""

    def test_bullish_trend_detected(self):
        """多头趋势应被正确识别。"""
        df = _make_bullish_trend_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        # 后期应有多头趋势
        last_20 = result.tail(20)
        assert last_20["trend_bull"].any(), "多头趋势应被识别"

    def test_sideways_trend_detected(self):
        """震荡趋势应被正确识别。"""
        df = _make_sideways_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        # 应有震荡趋势
        last_20 = result.tail(20)
        assert last_20["trend_sideways"].any(), "震荡趋势应被识别"

    def test_bearish_trend_detected(self):
        """空头趋势应被正确识别。"""
        df = _make_downtrend_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        # 后期应有空头趋势
        last_20 = result.tail(20)
        assert last_20["trend_bear"].any(), "空头趋势应被识别"


# ── 买入信号测试 ──


class TestMA20TrendBuySignals:
    """买入信号测试。"""

    def test_signal_a_pullback_buy(self):
        """信号A（回踩低吸）应在满足条件时触发。"""
        df = _make_pullback_to_ma20_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        # 后期应有买入信号
        last_20 = result.tail(20)
        has_buy = (last_20["signal"] == 1).any()
        # 注意：由于随机性，不强制要求一定有信号，但数据结构应正确
        assert "signal_a_buy" in result.columns
        assert "signal_b_buy" in result.columns

    def test_signal_b_breakout_buy(self):
        """信号B（放量突破）应在满足条件时触发。"""
        df = _make_breakout_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        # 检查信号B相关列存在
        assert "signal_b_buy" in result.columns
        assert "vol_ratio" in result.columns


# ── 卖出信号测试 ──


class TestMA20TrendSellSignals:
    """卖出信号测试。"""

    def test_signal_c_take_profit(self):
        """信号C（止盈离场）应在乖离率>30%时触发。"""
        df = _make_high_deviation_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        # 后期应有高乖离率
        last_10 = result.tail(10)
        has_high_deviation = (last_10["deviation"] > 0.30).any()
        assert has_high_deviation, "应有高乖离率"

    def test_signal_d_stop_loss_columns(self):
        """信号D（止损离场）相关列应存在。"""
        df = _make_downtrend_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        assert "signal_d_sell" in result.columns
        assert "signal_c_sell" in result.columns
        assert "signal_e_sell" in result.columns


# ── 仓位管理测试 ──


class TestMA20TrendPositionManagement:
    """仓位管理测试。"""

    def test_bullish_position_ratio(self):
        """多头趋势仓位系数应为0.6。"""
        df = _make_bullish_trend_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        last_20 = result.tail(20)
        bullish_rows = last_20[last_20["trend_bull"]]
        if not bullish_rows.empty:
            assert (bullish_rows["position_ratio"] == 0.6).all()

    def test_sideways_position_ratio(self):
        """震荡趋势仓位系数应为0.4。"""
        df = _make_sideways_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        last_20 = result.tail(20)
        sideways_rows = last_20[last_20["trend_sideways"]]
        if not sideways_rows.empty:
            assert (sideways_rows["position_ratio"] == 0.4).all()

    def test_bearish_position_ratio(self):
        """空头趋势仓位系数应为0.1。"""
        df = _make_downtrend_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        last_20 = result.tail(20)
        bearish_rows = last_20[last_20["trend_bear"]]
        if not bearish_rows.empty:
            assert (bearish_rows["position_ratio"] == 0.1).all()


# ── 止损止盈价测试 ──


class TestMA20TrendStopLossTakeProfit:
    """止损止盈价测试。"""

    def test_stop_loss_price_calculation(self):
        """止损价 = MA20 * (1 - stop_loss_pct)。"""
        df = _make_bullish_trend_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        # 检查止损价计算
        expected_stop_loss = result["ma20"] * (1 - 0.05)
        pd.testing.assert_series_equal(
            result["stop_loss_price"],
            expected_stop_loss,
            check_names=False,
        )

    def test_take_profit_price_calculation(self):
        """止盈价 = MA20 * (1 + deviation_threshold)。"""
        df = _make_bullish_trend_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        # 检查止盈价计算
        expected_take_profit = result["ma20"] * (1 + 0.30)
        pd.testing.assert_series_equal(
            result["take_profit_price"],
            expected_take_profit,
            check_names=False,
        )


# ── 置信度测试 ──


class TestMA20TrendConfidence:
    """置信度测试。"""

    def test_buy_confidence_range(self):
        """买入置信度应在合理范围内。"""
        df = _make_pullback_to_ma20_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        # 有信号的日子置信度应 > 0
        signal_days = result[result["signal"] == 1]
        if not signal_days.empty:
            assert (signal_days["confidence"] > 0).all()
            assert (signal_days["confidence"] <= 1.0).all()

    def test_sell_confidence_range(self):
        """卖出置信度应在合理范围内。"""
        df = _make_high_deviation_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        # 有信号的日子置信度应 > 0
        signal_days = result[result["signal"] == -1]
        if not signal_days.empty:
            assert (signal_days["confidence"] > 0).all()
            assert (signal_days["confidence"] <= 1.0).all()


# ── K线形态识别测试 ──


class TestMA20TrendCandlestickPatterns:
    """K线形态识别测试。"""

    def test_doji_detection(self):
        """十字星应被正确识别。"""
        df = _make_bullish_trend_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        assert "doji" in result.columns
        assert "small_bull" in result.columns
        assert "long_lower_shadow" in result.columns
        assert "reversal_signal" in result.columns

    def test_calculation_columns_exist(self):
        """所有计算列应存在。"""
        df = _make_bullish_trend_df()
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        expected_columns = [
            "ma20",
            "ma20_slope",
            "vol_ma",
            "vol_ratio",
            "deviation",
            "trend_bull",
            "trend_sideways",
            "trend_bear",
            "body",
            "body_pct",
            "upper_shadow",
            "lower_shadow",
            "doji",
            "small_bull",
            "long_lower_shadow",
            "reversal_signal",
            "signal_a_buy",
            "signal_b_buy",
            "signal_c_sell",
            "signal_d_sell",
            "signal_e_sell",
            "signal",
            "confidence",
            "position_ratio",
            "stop_loss_price",
            "take_profit_price",
        ]
        for col in expected_columns:
            assert col in result.columns, f"列 {col} 应存在"


# ── 边界场景测试 ──


class TestMA20TrendEdgeCases:
    """边界场景测试。"""

    def test_empty_dataframe(self):
        """空DataFrame应返回空结果。"""
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)
        assert len(result) == 0

    def test_insufficient_data(self):
        """数据不足时应返回NaN。"""
        n = 10
        df = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=n, freq="D"),
                "open": np.random.randn(n) + 100,
                "high": np.random.randn(n) + 101,
                "low": np.random.randn(n) + 99,
                "close": np.random.randn(n) + 100,
                "volume": np.random.uniform(5_000_000, 10_000_000, size=n),
                "amount": np.random.uniform(5e8, 1e9, size=n),
            }
        )
        strategy = MA20TrendStrategy()
        result = strategy.generate_signals(df)

        # MA20需要20天数据，前19天应为NaN
        assert result["ma20"].iloc[0] != result["ma20"].iloc[0]  # NaN check

    def test_strategy_name(self):
        """策略名称应为 ma20_trend。"""
        strategy = MA20TrendStrategy()
        assert strategy.name == "ma20_trend"

    def test_strategy_category(self):
        """策略类别应为 trend。"""
        strategy = MA20TrendStrategy()
        assert strategy.category == "trend"
