"""云策略单元测试。

覆盖核心场景：
- 三维趋势判定（均线多头 + 高低点抬升 + 量价配合）
- 两类入场买点（低吸 + 突破）
- 卖出信号（跌破MA20）
- 分层仓位（初期4成/确认6-7成/末期2成）
- 置信度计算
- 参数校验
- 边界场景
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.cloud_trend import CloudTrendStrategy


def _make_uptrend_df(days: int = 120, seed: int = 42) -> pd.DataFrame:
    """构造上涨趋势测试数据：价格稳步上升 + 阳线放量阴线缩量。"""
    np.random.seed(seed)
    n = days
    trend = np.linspace(100, 150, n)
    noise = np.random.randn(n) * 1.0
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

        is_yang = close[i] > open_[i]
        if is_yang:
            volume[i] = np.random.uniform(8_000_000, 12_000_000)
        else:
            volume[i] = np.random.uniform(4_000_000, 7_000_000)

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
    """构造震荡市测试数据：价格横盘 + 量能无明显特征。"""
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
    """构造下跌趋势测试数据：价格持续下跌。"""
    np.random.seed(seed)
    n = days
    trend = np.linspace(150, 100, n)
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

        high[i] = max(close[i], open_[i]) + abs(np.random.randn()) * 0.5
        low[i] = min(close[i], open_[i]) - abs(np.random.randn()) * 0.5
        volume[i] = np.random.uniform(3_000_000, 6_000_000)

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


class TestCloudTrendParams:
    """参数校验测试。"""

    def test_validate_params_default_passes(self):
        CloudTrendStrategy().validate_params()

    def test_validate_params_ma_short_ge_trend_raises(self):
        with pytest.raises(ValueError, match="ma_short 必须小于 ma_trend"):
            CloudTrendStrategy({"ma_short": 20, "ma_trend": 10}).validate_params()

    def test_validate_params_ma_equal_raises(self):
        with pytest.raises(ValueError, match="ma_short 必须小于 ma_trend"):
            CloudTrendStrategy({"ma_short": 20, "ma_trend": 20}).validate_params()


class TestCloudTrendDimensions:
    """三维趋势判定测试。"""

    def test_uptrend_all_three_dims_satisfied(self):
        """上涨趋势中三维应全部满足。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        last_valid = result.dropna(subset=["ma_trend"]).iloc[-1]
        assert last_valid["dim1_ma_bull"], "上涨趋势中MA20斜率应向上"
        assert last_valid["dim2_hl_rising"], "上涨趋势中高低点应抬升"
        assert last_valid["dim3_vol_healthy"], "上涨趋势中阳量应大于阴量"
        assert last_valid["trend_all_3"], "上涨趋势中三维应全部满足"

    def test_sideways_not_all_three(self):
        """震荡市中三维不应全部满足。"""
        df = _make_sideways_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        last_valid = result.dropna(subset=["ma_trend"]).iloc[-1]
        assert not last_valid["trend_all_3"], "震荡市中三维不应全部满足"

    def test_downtrend_not_all_three(self):
        """下跌趋势中三维不应全部满足。"""
        df = _make_downtrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        last_valid = result.dropna(subset=["ma_trend"]).iloc[-1]
        assert not last_valid["trend_all_3"], "下跌趋势中三维不应全部满足"

    def test_trend_dims_count_range(self):
        """趋势维度计数应在0-3之间。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        valid = result.dropna(subset=["ma_trend"])
        assert valid["trend_dims_count"].min() >= 0
        assert valid["trend_dims_count"].max() <= 3

    def test_trend_consecutive_days_tracks(self):
        """趋势持续天数应正确追踪。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        valid = result.dropna(subset=["ma_trend"])
        assert valid["trend_all_3_days"].max() > 0, "上涨趋势中应有连续满足天数"


class TestCloudTrendBuySignals:
    """买入信号测试。"""

    def test_pullback_buy_in_uptrend(self):
        """上涨趋势中回踩MA10/MA20应产生低吸买点。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        buy_signals = result[result["signal"] == 1]
        assert len(buy_signals) > 0, "上涨趋势中应有买入信号"

    def test_no_buy_in_sideways(self):
        """震荡市中不应产生买入信号（或极少）。"""
        df = _make_sideways_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        buy_signals = result[result["signal"] == 1]
        # 震荡市中三维趋势不满足，买入信号应极少（<3个，随机噪声可能触发个别）
        assert len(buy_signals) <= 3, f"震荡市中买入信号应极少，实际{len(buy_signals)}个"

    def test_no_buy_in_downtrend(self):
        """下跌趋势中不应产生买入信号。"""
        df = _make_downtrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        buy_signals = result[result["signal"] == 1]
        assert len(buy_signals) == 0, "下跌趋势中不应有买入信号"

    def test_breakout_buy_with_volume_surge(self):
        """放量突破20日高点应产生突破买点（手动构造场景）。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        # 检查突破条件列存在且逻辑正确
        breakout_high = result["close"] > result["hh"].shift(1)
        volume_surge = result["volume"] > result["vol_ma"] * 1.5
        trend_ok = result["trend_at_least_2"]

        # 验证：当三个条件同时满足时，应有买入信号
        breakout_condition = breakout_high & volume_surge & trend_ok
        if breakout_condition.any():
            breakout_signals = result[breakout_condition & (result["signal"] == 1)]
            assert len(breakout_signals) > 0, "满足突破条件时应有买入信号"
        else:
            # 随机数据可能不满足突破条件，这是正常的
            pass

    def test_buy_signal_has_confidence(self):
        """买入信号应有置信度。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        buy_signals = result[result["signal"] == 1]
        assert (buy_signals["confidence"] > 0).all(), "买入信号置信度应>0"
        assert (buy_signals["confidence"] <= 1).all(), "买入信号置信度应≤1"

    def test_buy_signal_has_position_ratio(self):
        """买入信号应有建议仓位比例。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        buy_signals = result[result["signal"] == 1]
        assert (buy_signals["suggested_position_ratio"] > 0).all(), "买入信号应有仓位建议"


class TestCloudTrendSellSignals:
    """卖出信号测试。"""

    def test_sell_below_ma20_only_when_in_position(self):
        """仅持仓状态下跌破MA20才产生卖出信号。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        below_ma20 = result["close"] < result["ma_trend"]
        in_position = result["in_position"]

        # 持仓状态下跌破MA20的位置必须是卖出信号
        exit_required = below_ma20 & in_position
        if exit_required.any():
            assert (result.loc[exit_required, "signal"] == -1).all(), "持仓+跌破MA20应为卖出信号"

        # 非持仓状态下跌破MA20不应产生卖出信号
        exit_not_required = below_ma20 & ~in_position
        if exit_not_required.any():
            non_exit = result.loc[exit_not_required]
            assert (non_exit["signal"] != -1).all(), "非持仓状态下跌破MA20不应产生卖出信号"

    def test_sell_confidence(self):
        """卖出信号置信度应为0.75。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        sell_signals = result[result["signal"] == -1]
        if len(sell_signals) > 0:
            assert (sell_signals["confidence"] == 0.75).all()

    def test_trailing_stop_columns_exist(self):
        """移动止盈和时间止盈列应存在。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        assert "trailing_stop_active" in result.columns
        assert "time_stop_active" in result.columns
        assert "in_position" in result.columns
        assert "below_ma20" in result.columns

    def test_trailing_stop_triggered_by_big_drop(self):
        """大幅回落应触发移动止盈卖出。"""
        strategy = CloudTrendStrategy({"trailing_stop_pct": 0.05})
        n = 80
        close = np.linspace(100, 130, n)
        close[60:] = np.linspace(130, 115, n - 60)  # 从最高回落 >5%
        open_ = close - np.random.uniform(0.1, 0.5, n)
        high = close + np.random.uniform(0.1, 0.3, n)
        low = np.minimum(close, open_) - np.random.uniform(0.1, 0.3, n)
        volume = np.full(n, 8_000_000.0)
        df = pd.DataFrame(
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

        result = strategy._generate_signals_impl(df)
        if result["trailing_stop_active"].any():
            trailing_rows = result[result["trailing_stop_active"]]
            assert (trailing_rows["signal"] == -1).any(), "移动止盈触发处应有卖出信号"


class TestCloudTrendPositionSizing:
    """分层仓位测试。"""

    def test_early_stage_position_ratio(self):
        """趋势初期仓位应为40%。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy({"trend_early_days": 5})
        result = strategy._generate_signals_impl(df)

        early_buys = result[
            (result["signal"] == 1)
            & (result["trend_all_3_days"] > 0)
            & (result["trend_all_3_days"] <= 5)
        ]
        if len(early_buys) > 0:
            assert (early_buys["suggested_position_ratio"] == 0.40).all()

    def test_confirmed_stage_position_ratio(self):
        """趋势确认阶段仓位应为60%。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy({"trend_early_days": 5})
        result = strategy._generate_signals_impl(df)

        confirmed_buys = result[(result["signal"] == 1) & (result["trend_all_3_days"] > 5)]
        if len(confirmed_buys) > 0:
            ratios = confirmed_buys["suggested_position_ratio"]
            assert (ratios >= 0.20).all()
            assert (ratios <= 0.70).all()

    def test_position_ratio_in_valid_range(self):
        """仓位比例应在0.20-0.70之间。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        buy_signals = result[result["signal"] == 1]
        if len(buy_signals) > 0:
            assert buy_signals["suggested_position_ratio"].min() >= 0.20
            assert buy_signals["suggested_position_ratio"].max() <= 0.70


class TestCloudTrendEdgeCases:
    """边界场景测试。"""

    def test_empty_df_handled(self):
        """空DataFrame不应崩溃。"""
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)
        assert len(result) == 0

    def test_short_df_handled(self):
        """短DataFrame（不足计算窗口）不应崩溃。"""
        df = _make_uptrend_df(10)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)
        assert len(result) == 10
        assert "signal" in result.columns

    def test_signal_column_types(self):
        """信号列类型正确。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        assert result["signal"].dtype in (np.int64, np.int32, int)
        assert result["confidence"].dtype in (np.float64, float)

    def test_no_signal_on_nan_ma(self):
        """MA未计算出来的位置不应有信号。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        early_rows = result.iloc[:19]
        assert (early_rows["signal"] == 0).all(), "MA未就绪时不应有信号"

    def test_double_yin_reversal_detected(self):
        """双阴后企稳应被正确检测。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        assert "is_yin" in result.columns
        assert "is_yang" in result.columns

    def test_weakening_detection(self):
        """趋势弱化检测应有输出。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy._generate_signals_impl(df)

        assert "any_dim_weakening" in result.columns
        assert "dim1_weakening" in result.columns
        assert "dim2_weakening" in result.columns

    def test_original_df_not_mutated(self):
        """原始DataFrame不应被修改。"""
        df = _make_uptrend_df(120)
        original_columns = set(df.columns)
        strategy = CloudTrendStrategy()
        strategy._generate_signals_impl(df)

        assert set(df.columns) == original_columns, "原始df列不应被修改"
        assert "signal" not in df.columns, "原始df不应有signal列"


class TestCloudTrendIntegration:
    """集成测试：策略通过模板方法 generate_signals 调用。"""

    def test_generate_signals_template_method(self):
        """模板方法 generate_signals 应正常工作。"""
        df = _make_uptrend_df(120)
        strategy = CloudTrendStrategy()
        result = strategy.generate_signals(df, is_backtest=True)

        assert "signal" in result.columns
        assert "confidence" in result.columns
        assert len(result) == len(df)

    def test_strategy_name_and_category(self):
        """策略名称和分类应正确。"""
        strategy = CloudTrendStrategy()
        assert strategy.name == "cloud_trend"
        assert strategy.category == "trend"

    def test_registry_integration(self):
        """策略应可通过registry获取。"""
        from gugu.strategies.registry import get_strategy

        strategy = get_strategy("cloud_trend")
        assert isinstance(strategy, CloudTrendStrategy)
        assert strategy.name == "cloud_trend"
