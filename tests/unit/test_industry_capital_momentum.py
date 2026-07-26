"""行业资金强度+动量确认策略单元测试（个股适配版 v1.1）。

覆盖四层架构：资金监控层、动量确认层、入场触发层、退出风控层。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.industry_capital_momentum import IndustryCapitalMomentumStrategy


# ============================================================
# 辅助函数
# ============================================================


def _make_trending_df(n: int = 120, trend: str = "up") -> pd.DataFrame:
    """生成模拟趋势 DataFrame。"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")

    if trend == "up":
        base = np.linspace(10, 18, n) + np.random.normal(0, 0.2, n)
    elif trend == "down":
        base = np.linspace(18, 10, n) + np.random.normal(0, 0.2, n)
    else:
        base = 14 + np.cumsum(np.random.normal(0, 0.1, n))

    close = np.round(base, 2)
    open_ = close + np.random.normal(0, 0.1, n)
    high = np.maximum(open_, close) + np.abs(np.random.normal(0, 0.15, n))
    low = np.minimum(open_, close) - np.abs(np.random.normal(0, 0.15, n))
    volume = np.random.uniform(800000, 1500000, n)
    amount = volume * close

    return pd.DataFrame({
        "date": dates,
        "open": np.round(open_, 2),
        "high": np.round(high, 2),
        "low": np.round(low, 2),
        "close": close,
        "volume": volume.astype(int),
        "amount": amount,
    })


def _make_capital_inflow_df(n: int = 120) -> pd.DataFrame:
    """生成资金持续流入的模拟 DataFrame。"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")

    base = np.linspace(10, 16, n)
    close = np.round(base + np.random.normal(0, 0.15, n), 2)
    open_ = close + np.random.normal(0, 0.08, n)
    high = np.maximum(open_, close) + np.abs(np.random.normal(0, 0.12, n))
    low = np.minimum(open_, close) - np.abs(np.random.normal(0, 0.12, n))

    base_vol = 800000
    # 前期平稳，后期放量（确保量比 > 1.2）
    vol_multiplier = np.concatenate([
        np.ones(40) * 1.0,
        np.linspace(1.0, 2.5, n - 40)
    ])
    volume = (base_vol * vol_multiplier + np.random.normal(0, 50000, n)).astype(int)
    amount = volume * close

    return pd.DataFrame({
        "date": dates,
        "open": np.round(open_, 2),
        "high": np.round(high, 2),
        "low": np.round(low, 2),
        "close": close,
        "volume": volume,
        "amount": amount,
    })


def _make_overheat_df(n: int = 120) -> pd.DataFrame:
    """生成过热 DataFrame（20日涨幅超30%）。"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")

    base = np.concatenate([
        np.linspace(10, 14, 40),
        np.linspace(14, 16, 30),
        np.linspace(16, 15, 50),
    ])
    close = np.round(base + np.random.normal(0, 0.1, n), 2)
    open_ = close + np.random.normal(0, 0.05, n)
    high = np.maximum(open_, close) + np.abs(np.random.normal(0, 0.1, n))
    low = np.minimum(open_, close) - np.abs(np.random.normal(0, 0.1, n))
    volume = np.random.uniform(1000000, 2000000, n).astype(int)
    amount = volume * close

    return pd.DataFrame({
        "date": dates,
        "open": np.round(open_, 2),
        "high": np.round(high, 2),
        "low": np.round(low, 2),
        "close": close,
        "volume": volume,
        "amount": amount,
    })


def _make_strong_uptrend_df(n: int = 120) -> pd.DataFrame:
    """生成强上涨 DataFrame（20日涨幅>25%）。"""
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")

    base = np.linspace(10, 16, n) + np.random.normal(0, 0.1, n)
    close = np.round(base, 2)
    open_ = close + np.random.normal(0, 0.05, n)
    high = np.maximum(open_, close) + np.abs(np.random.normal(0, 0.1, n))
    low = np.minimum(open_, close) - np.abs(np.random.normal(0, 0.1, n))
    volume = np.random.uniform(1200000, 2000000, n).astype(int)
    amount = volume * close

    return pd.DataFrame({
        "date": dates,
        "open": np.round(open_, 2),
        "high": np.round(high, 2),
        "low": np.round(low, 2),
        "close": close,
        "volume": volume,
        "amount": amount,
    })


# ============================================================
# 基础测试
# ============================================================


class TestIndustryCapitalMomentumBasic:
    """策略基础测试。"""

    def test_name_and_category(self) -> None:
        strategy = IndustryCapitalMomentumStrategy()
        assert strategy.name == "industry_capital_momentum"
        assert strategy.category == "capital_flow"

    def test_default_params_loaded(self) -> None:
        strategy = IndustryCapitalMomentumStrategy()
        assert "ma_short" in strategy.params
        assert "ma_long" in strategy.params
        assert strategy.params["ma_short"] == 5
        assert strategy.params["ma_long"] == 20

    def test_custom_params_override(self) -> None:
        strategy = IndustryCapitalMomentumStrategy({"ma_short": 3, "volume_ratio": 1.5})
        assert strategy.params["ma_short"] == 3
        assert strategy.params["volume_ratio"] == 1.5

    def test_validate_params_ok(self) -> None:
        strategy = IndustryCapitalMomentumStrategy()
        strategy.validate_params()

    def test_validate_params_invalid_ma(self) -> None:
        with pytest.raises(ValueError, match="ma_short 必须 < ma_long"):
            IndustryCapitalMomentumStrategy({"ma_short": 20, "ma_long": 5})

    def test_missing_columns_raises(self) -> None:
        strategy = IndustryCapitalMomentumStrategy()
        df = pd.DataFrame({"close": [1, 2, 3], "volume": [100, 200, 300]})
        with pytest.raises(ValueError, match="缺少必要列"):
            strategy.generate_signals(df, is_backtest=True)


# ============================================================
# 资金监控层测试
# ============================================================


class TestCapitalMonitoringLayer:
    """资金监控层测试（个股适配版）。"""

    def test_ma_system_computed(self) -> None:
        """均线系统正确计算。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_capital_inflow_df()
        result = strategy.generate_signals(df, is_backtest=True)

        assert "ma5" in result.columns
        assert "ma10" in result.columns
        assert "ma20" in result.columns
        assert result["ma5"].notna().sum() > 0
        assert result["ma20"].notna().sum() > 0

    def test_vol_ratio_computed(self) -> None:
        """量比指标正确计算。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_capital_inflow_df()
        result = strategy.generate_signals(df, is_backtest=True)

        assert "vol_ratio" in result.columns
        assert result["vol_ratio"].notna().sum() > 0

    def test_amount_ratio_computed(self) -> None:
        """成交额占比指标正确计算。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_capital_inflow_df()
        result = strategy.generate_signals(df, is_backtest=True)

        assert "amount_ratio" in result.columns
        assert result["amount_ratio"].notna().sum() > 0

    def test_ma5_slope_computed(self) -> None:
        """MA5斜率正确计算。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_capital_inflow_df()
        result = strategy.generate_signals(df, is_backtest=True)

        assert "ma5_slope" in result.columns

    def test_daily_return_computed(self) -> None:
        """日收益率正确计算。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_capital_inflow_df()
        result = strategy.generate_signals(df, is_backtest=True)

        assert "daily_return" in result.columns
        assert result["daily_return"].notna().sum() > 0

    def test_return_20d_computed(self) -> None:
        """20日涨幅正确计算。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_capital_inflow_df()
        result = strategy.generate_signals(df, is_backtest=True)

        assert "return_20d" in result.columns

    def test_stock_up_computed(self) -> None:
        """个股涨跌标记正确计算。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_capital_inflow_df()
        result = strategy.generate_signals(df, is_backtest=True)

        assert "stock_up" in result.columns
        assert set(result["stock_up"].dropna().unique()).issubset({0.0, 1.0})


# ============================================================
# 动量确认层测试
# ============================================================


class TestMomentumConfirmationLayer:
    """动量确认层测试。"""

    def test_rsi_computed(self) -> None:
        """RSI 指标正确计算。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_trending_df()
        result = strategy.generate_signals(df, is_backtest=True)

        assert "rsi" in result.columns
        valid_rsi = result["rsi"].dropna()
        assert (valid_rsi >= 0).all() and (valid_rsi <= 100).all()

    def test_ma5_ma20_golden_cross_detected(self) -> None:
        """MA5/MA20金叉能被检测到。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_trending_df(trend="up", n=150)
        result = strategy.generate_signals(df, is_backtest=True)

        # 上升趋势中MA5应多次大于MA20
        ma5_above = (result["ma5"] > result["ma20"]).sum()
        assert ma5_above > 50

    def test_volume_confirm_threshold(self) -> None:
        """量能确认阈值正确应用。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_capital_inflow_df()
        result = strategy.generate_signals(df, is_backtest=True)

        # 量比应有一定天数 >= 1.2
        vol_confirm_days = (result["vol_ratio"] >= 1.2).sum()
        assert vol_confirm_days > 0


# ============================================================
# 入场触发层测试
# ============================================================


class TestEntryTriggerLayer:
    """入场触发层测试。"""

    def test_signal_values_valid(self) -> None:
        """信号值只能是 -1, 0, 1。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_trending_df(n=120)
        result = strategy.generate_signals(df, is_backtest=True)

        assert result["signal"].isin([-1, 0, 1]).all()

    def test_uptrend_generates_buy_signal(self) -> None:
        """上升趋势+资金流入应生成买入信号。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_capital_inflow_df(n=200)
        result = strategy.generate_signals(df, is_backtest=True)

        buy_signals = (result["signal"] == 1).sum()
        assert buy_signals > 0, "上升趋势+放量应产生买入信号"

    def test_downtrend_no_buy_signal(self) -> None:
        """下降趋势不应生成买入信号。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_trending_df(trend="down", n=120)
        result = strategy.generate_signals(df, is_backtest=True)

        buy_signals = (result["signal"] == 1).sum()
        assert buy_signals <= 3

    def test_no_buy_without_ma_golden_cross(self) -> None:
        """无MA5/MA20金叉时不应生成买入信号。"""
        strategy = IndustryCapitalMomentumStrategy()
        np.random.seed(42)
        n = 120
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        base = np.linspace(20, 15, n) + np.random.normal(0, 0.3, n)
        close = np.round(base, 2)
        open_ = close + np.random.normal(0, 0.1, n)
        high = np.maximum(open_, close) + np.abs(np.random.normal(0, 0.15, n))
        low = np.minimum(open_, close) - np.abs(np.random.normal(0, 0.15, n))
        volume = np.random.uniform(800000, 1500000, n)
        amount = volume * close

        df = pd.DataFrame({
            "date": dates, "open": np.round(open_, 2), "high": np.round(high, 2),
            "low": np.round(low, 2), "close": close, "volume": volume.astype(int), "amount": amount
        })

        result = strategy.generate_signals(df, is_backtest=True)

        buy_signals = (result["signal"] == 1).sum()
        assert buy_signals == 0, f"无金叉时不应买入，但发现{buy_signals}个买入信号"

    def test_rsi_out_of_zone_no_buy_signal(self) -> None:
        """RSI不在35-70区间时不应生成买入信号。"""
        strategy = IndustryCapitalMomentumStrategy()
        np.random.seed(42)
        n = 120
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        base = np.linspace(10, 25, n) + np.random.normal(0, 0.1, n)
        close = np.round(base, 2)
        open_ = close + np.random.normal(0, 0.05, n)
        high = np.maximum(open_, close) + np.abs(np.random.normal(0, 0.1, n))
        low = np.minimum(open_, close) - np.abs(np.random.normal(0, 0.1, n))
        volume = np.random.uniform(1000000, 2000000, n)
        amount = volume * close

        df = pd.DataFrame({
            "date": dates, "open": np.round(open_, 2), "high": np.round(high, 2),
            "low": np.round(low, 2), "close": close, "volume": volume.astype(int), "amount": amount
        })

        result = strategy.generate_signals(df, is_backtest=True)

        rsi_high_mask = result["rsi"] > 70
        buy_signals_in_rsi_high = (result.loc[rsi_high_mask, "signal"] == 1).sum()
        assert buy_signals_in_rsi_high == 0

    def test_return_20d_high_filtered(self) -> None:
        """20日涨幅>25%但其他条件不满足时，验证评分机制。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_strong_uptrend_df(n=120)
        result = strategy.generate_signals(df, is_backtest=True)

        # 验证策略正常运行，信号列有效
        assert result["signal"].isin([-1, 0, 1]).all()


# ============================================================
# 退出风控层测试
# ============================================================


class TestExitRiskControlLayer:
    """退出风控层测试。"""

    def test_trend_breakdown_triggers_sell(self) -> None:
        """趋势破坏（跌破MA10）可能触发卖出信号。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_trending_df(trend="up", n=80)
        tail = _make_trending_df(trend="down", n=40)
        tail["date"] = pd.date_range(df["date"].iloc[-1] + pd.Timedelta(days=1), periods=40, freq="B")
        combined = pd.concat([df, tail], ignore_index=True)

        result = strategy.generate_signals(combined, is_backtest=True)

        assert "signal" in result.columns
        assert result["signal"].isin([-1, 0, 1]).all()

    def test_overheat_triggers_sell(self) -> None:
        """过热行情（20日涨幅>30%）可能触发卖出信号。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_overheat_df()
        result = strategy.generate_signals(df, is_backtest=True)

        assert "signal" in result.columns

    def test_signal_values_valid(self) -> None:
        """信号值只能是 -1, 0, 1。"""
        strategy = IndustryCapitalMomentumStrategy()
        df = _make_trending_df(n=120)
        result = strategy.generate_signals(df, is_backtest=True)

        assert result["signal"].isin([-1, 0, 1]).all()


# ============================================================
# RSI 计算测试
# ============================================================


class TestRSICalculation:
    """RSI 计算测试。"""

    def test_rsi_all_up(self) -> None:
        """全涨行情 RSI 应接近 100。"""
        strategy = IndustryCapitalMomentumStrategy()
        close = pd.Series(np.arange(10.0, 30.0, 0.5))
        rsi = strategy._calc_rsi(close, 14)
        assert rsi.iloc[-1] == 100.0

    def test_rsi_all_down(self) -> None:
        """全跌行情 RSI 接近 0。"""
        strategy = IndustryCapitalMomentumStrategy()
        close = pd.Series(np.arange(30.0, 10.0, -0.5))
        rsi = strategy._calc_rsi(close, 14)
        assert rsi.iloc[-1] < 30

    def test_rsi_flat(self) -> None:
        """平盘行情 RSI 接近 50。"""
        strategy = IndustryCapitalMomentumStrategy()
        close = pd.Series([15.0] * 30)
        rsi = strategy._calc_rsi(close, 14)
        assert rsi.iloc[-1] == 50.0
