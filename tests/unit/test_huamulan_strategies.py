"""花木兰策略系列单元测试。"""

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.huamulan_tail_pick import HuamulanTailPickStrategy
from gugu.strategies.huamulan_crash_layout import HuamulanCrashLayoutStrategy
from gugu.strategies.huamulan_fake_dip import HuamulanFakeDipStrategy


@pytest.fixture
def tail_pick_strategy() -> HuamulanTailPickStrategy:
    return HuamulanTailPickStrategy()


@pytest.fixture
def crash_layout_strategy() -> HuamulanCrashLayoutStrategy:
    return HuamulanCrashLayoutStrategy()


@pytest.fixture
def fake_dip_strategy() -> HuamulanFakeDipStrategy:
    return HuamulanFakeDipStrategy()


@pytest.fixture
def sample_data() -> pd.DataFrame:
    """创建样本日线数据（约100个交易日）。"""
    np.random.seed(42)
    dates = pd.date_range(start="2024-01-01", periods=100, freq="B")

    base_price = 10.0
    prices = [base_price]
    for i in range(1, 100):
        change = np.random.normal(0.002, 0.02)
        prices.append(prices[-1] * (1 + change))

    prices = np.array(prices)
    base_volume = 1000000
    volumes = base_volume * (1 + np.random.randn(100) * 0.3)
    volumes = np.abs(volumes)

    df = pd.DataFrame(
        {
            "date": dates,
            "open": prices * (1 + np.random.randn(100) * 0.005),
            "high": prices * (1 + np.abs(np.random.randn(100) * 0.015)),
            "low": prices * (1 - np.abs(np.random.randn(100) * 0.015)),
            "close": prices,
            "volume": volumes,
            "amount": volumes * prices,
        }
    )

    df["high"] = df[["open", "high", "close"]].max(axis=1) * 1.001
    df["low"] = df[["open", "low", "close"]].min(axis=1) * 0.999

    return df


class TestHuamulanTailPickStrategy:
    """花木兰尾盘选股六步法测试。"""

    def test_strategy_name_and_category(self, tail_pick_strategy: HuamulanTailPickStrategy) -> None:
        assert tail_pick_strategy.name == "huamulan_tail_pick"
        assert tail_pick_strategy.category == "trend"

    def test_generate_signals_returns_dataframe(
        self, tail_pick_strategy: HuamulanTailPickStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = tail_pick_strategy.generate_signals(sample_data)
        assert isinstance(result, pd.DataFrame)

    def test_signal_column_exists(
        self, tail_pick_strategy: HuamulanTailPickStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = tail_pick_strategy.generate_signals(sample_data)
        assert "signal" in result.columns
        assert "confidence" in result.columns

    def test_signal_values_valid(
        self, tail_pick_strategy: HuamulanTailPickStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = tail_pick_strategy.generate_signals(sample_data)
        assert result["signal"].isin([-1, 0, 1]).all()

    def test_confidence_range(
        self, tail_pick_strategy: HuamulanTailPickStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = tail_pick_strategy.generate_signals(sample_data)
        assert result["confidence"].between(0, 1).all()

    def test_no_signals_on_empty_data(self, tail_pick_strategy: HuamulanTailPickStrategy) -> None:
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
        try:
            result = tail_pick_strategy.generate_signals(df)
            if len(result) > 0:
                assert result["signal"].eq(0).all()
        except (ValueError, KeyError):
            pass

    def test_validate_params_valid(self, tail_pick_strategy: HuamulanTailPickStrategy) -> None:
        tail_pick_strategy.validate_params()

    def test_validate_params_invalid_gain(self) -> None:
        with pytest.raises(ValueError, match="gain_min_pct 必须小于 gain_max_pct"):
            HuamulanTailPickStrategy({"gain_min_pct": 0.05, "gain_max_pct": 0.03})

    def test_validate_params_invalid_turnover(self) -> None:
        with pytest.raises(ValueError, match="turnover_min_pct 必须小于 turnover_max_pct"):
            HuamulanTailPickStrategy({"turnover_min_pct": 0.10, "turnover_max_pct": 0.05})

    # ── v0.8.0 回测基建修复：真实市值/换手率注入 ──

    def test_real_market_cap_used_when_injected(self) -> None:
        """数据层注入真实 market_cap 时使用真实值（不再 close*1e8 假市值）。"""
        dates = pd.date_range(start="2024-01-01", periods=30, freq="B")
        prices = np.linspace(10.0, 11.0, 30)
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices,
                "high": prices * 1.01,
                "low": prices * 0.99,
                "close": prices,
                "volume": 1000000,
                "amount": 1000000 * prices,
            }
        )
        # 注入真实流通市值 80亿（符合50-200亿区间）+ 真实换手率 7%
        df["market_cap"] = 80e8
        df["turnover_rate"] = 0.07
        # 最后一天构造买入信号：涨幅4% + 量比>1 + 换手7% + 市值80亿 + 均线多头 + 量能放大
        # 量能阶梯放大需连续3日递增：第27/28/29天 volume 递增
        df.loc[27, "volume"] = 900000
        df.loc[28, "volume"] = 1000000
        df.loc[29, "volume"] = 1300000
        df.loc[29, "close"] = df.loc[28, "close"] * 1.04
        result = HuamulanTailPickStrategy().generate_signals(df)
        assert result.loc[29, "signal"] == 1

    def test_missing_market_cap_auto_passes(self) -> None:
        """无真实市值列时市值条件自动放行（不因假数据误杀）。"""
        dates = pd.date_range(start="2024-01-01", periods=30, freq="B")
        prices = np.linspace(10.0, 11.0, 30)
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices,
                "high": prices * 1.01,
                "low": prices * 0.99,
                "close": prices,
                "volume": 1000000,
                "amount": 1000000 * prices,
            }
        )
        # 不注入 market_cap，仅注入换手率
        df["turnover_rate"] = 0.07
        df.loc[27, "volume"] = 900000
        df.loc[28, "volume"] = 1000000
        df.loc[29, "volume"] = 1300000
        df.loc[29, "close"] = df.loc[28, "close"] * 1.04
        result = HuamulanTailPickStrategy().generate_signals(df)
        # 无市值数据 → 市值条件自动放行，其余条件满足应触发
        assert result.loc[29, "signal"] == 1


class TestHuamulanCrashLayoutStrategy:
    """花木兰空仓等暴跌布局法测试。"""

    def test_strategy_name_and_category(
        self, crash_layout_strategy: HuamulanCrashLayoutStrategy
    ) -> None:
        assert crash_layout_strategy.name == "huamulan_crash_layout"
        assert crash_layout_strategy.category == "trend"

    def test_generate_signals_returns_dataframe(
        self, crash_layout_strategy: HuamulanCrashLayoutStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = crash_layout_strategy.generate_signals(sample_data)
        assert isinstance(result, pd.DataFrame)

    def test_signal_column_exists(
        self, crash_layout_strategy: HuamulanCrashLayoutStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = crash_layout_strategy.generate_signals(sample_data)
        assert "signal" in result.columns
        assert "confidence" in result.columns

    def test_signal_values_valid(
        self, crash_layout_strategy: HuamulanCrashLayoutStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = crash_layout_strategy.generate_signals(sample_data)
        assert result["signal"].isin([-1, 0, 1]).all()

    def test_confidence_range(
        self, crash_layout_strategy: HuamulanCrashLayoutStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = crash_layout_strategy.generate_signals(sample_data)
        assert result["confidence"].between(0, 1).all()

    def test_validate_params_valid(
        self, crash_layout_strategy: HuamulanCrashLayoutStrategy
    ) -> None:
        crash_layout_strategy.validate_params()

    def test_validate_params_invalid_crash_threshold(self) -> None:
        with pytest.raises(ValueError, match="crash_threshold 必须为负数"):
            HuamulanCrashLayoutStrategy({"crash_threshold": 0.03})

    def test_validate_params_invalid_target_gain(self) -> None:
        with pytest.raises(ValueError, match="target_gain 必须大于 0"):
            HuamulanCrashLayoutStrategy({"target_gain": -0.15})

    # ── v0.8.0 回测基建修复：真实市值/PE/指数列注入 ──

    def test_real_pe_used_when_injected(self) -> None:
        """数据层注入真实 PE 时使用真实值（不再 pe=25 假值）。"""
        dates = pd.date_range(start="2024-01-01", periods=40, freq="B")
        prices = np.linspace(10.0, 12.0, 40)
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices,
                "high": prices * 1.01,
                "low": prices * 0.99,
                "close": prices,
                "volume": 1000000,
                "amount": 1000000 * prices,
            }
        )
        # 注入真实市值 300亿（>200亿龙头）+ 真实PE 20（<30）
        df["market_cap"] = 300e8
        df["pe"] = 20.0
        # 构造：第36天指数暴跌4%（<-3%）+ 个股跌5%（被错杀），
        # 第37-39天企稳（不再创新低）→ 第39天触发买入
        df["index_return"] = -0.04
        df.loc[36, "close"] = df.loc[35, "close"] * 0.95  # 个股跌5%
        df.loc[36, "low"] = df.loc[36, "close"] * 0.98
        for i in (37, 38, 39):
            df.loc[i, "close"] = df.loc[36, "close"] * (1 + 0.005 * (i - 36))
            df.loc[i, "low"] = df.loc[36, "low"] * (1 + 0.002 * (i - 36))
        result = HuamulanCrashLayoutStrategy().generate_signals(df)
        assert result.loc[39, "signal"] == 1

    def test_missing_pe_auto_passes(self) -> None:
        """无真实 PE 列时估值条件自动放行（不因假 PE=25 误判）。"""
        dates = pd.date_range(start="2024-01-01", periods=40, freq="B")
        prices = np.linspace(10.0, 12.0, 40)
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices,
                "high": prices * 1.01,
                "low": prices * 0.99,
                "close": prices,
                "volume": 1000000,
                "amount": 1000000 * prices,
            }
        )
        df["market_cap"] = 300e8  # 注入市值但不注入PE
        df["index_return"] = -0.04
        df.loc[36, "close"] = df.loc[35, "close"] * 0.95
        df.loc[36, "low"] = df.loc[36, "close"] * 0.98
        for i in (37, 38, 39):
            df.loc[i, "close"] = df.loc[36, "close"] * (1 + 0.005 * (i - 36))
            df.loc[i, "low"] = df.loc[36, "low"] * (1 + 0.002 * (i - 36))
        result = HuamulanCrashLayoutStrategy().generate_signals(df)
        assert result.loc[39, "signal"] == 1

    def test_uses_index_return_column(self) -> None:
        """指数列注入后使用真实指数跌幅（不再走个股跌5%降级分支）。"""
        dates = pd.date_range(start="2024-01-01", periods=40, freq="B")
        prices = np.linspace(10.0, 12.0, 40)
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices,
                "high": prices * 1.01,
                "low": prices * 0.99,
                "close": prices,
                "volume": 1000000,
                "amount": 1000000 * prices,
            }
        )
        df["market_cap"] = 300e8
        df["pe"] = 20.0
        # 指数跌4%触发暴跌，但个股只跌2%（未被错杀）→ 不触发
        df["index_return"] = -0.04
        df.loc[36, "close"] = df.loc[35, "close"] * 0.98
        df.loc[36, "low"] = df.loc[36, "close"] * 0.99
        for i in (37, 38, 39):
            df.loc[i, "close"] = df.loc[36, "close"] * (1 + 0.005 * (i - 36))
            df.loc[i, "low"] = df.loc[36, "low"] * (1 + 0.002 * (i - 36))
        result = HuamulanCrashLayoutStrategy().generate_signals(df)
        assert result.loc[39, "signal"] != 1


class TestHuamulanFakeDipStrategy:
    """花木兰早盘假摔低吸战法测试。"""

    def test_strategy_name_and_category(self, fake_dip_strategy: HuamulanFakeDipStrategy) -> None:
        assert fake_dip_strategy.name == "huamulan_fake_dip"
        assert fake_dip_strategy.category == "mean_revert"

    def test_generate_signals_returns_dataframe(
        self, fake_dip_strategy: HuamulanFakeDipStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = fake_dip_strategy.generate_signals(sample_data)
        assert isinstance(result, pd.DataFrame)

    def test_signal_column_exists(
        self, fake_dip_strategy: HuamulanFakeDipStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = fake_dip_strategy.generate_signals(sample_data)
        assert "signal" in result.columns
        assert "confidence" in result.columns

    def test_signal_values_valid(
        self, fake_dip_strategy: HuamulanFakeDipStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = fake_dip_strategy.generate_signals(sample_data)
        assert result["signal"].isin([-1, 0, 1]).all()

    def test_confidence_range(
        self, fake_dip_strategy: HuamulanFakeDipStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = fake_dip_strategy.generate_signals(sample_data)
        assert result["confidence"].between(0, 1).all()

    def test_validate_params_valid(self, fake_dip_strategy: HuamulanFakeDipStrategy) -> None:
        fake_dip_strategy.validate_params()

    def test_validate_params_invalid_shadow(self) -> None:
        with pytest.raises(ValueError, match="lower_shadow_min_pct 必须大于 0"):
            HuamulanFakeDipStrategy({"lower_shadow_min_pct": -0.03})

    def test_validate_params_invalid_vol_shrink(self) -> None:
        with pytest.raises(ValueError, match="vol_shrink_ratio 必须大于 0"):
            HuamulanFakeDipStrategy({"vol_shrink_ratio": -1.0})

    # ── v0.8.0 重构专项测试：忠实原文核心3条件（长下影线>3% + 收红 + 缩量） ──

    def test_buy_signal_requires_three_core_conditions(self) -> None:
        """买入 = 长下影线>3% + 收阳 + 缩量，三条件缺一不可。"""
        dates = pd.date_range(start="2024-01-01", periods=30, freq="B")
        # 构造：前5日均量基线
        base = pd.DataFrame(
            {
                "date": dates,
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 1000000,
                "amount": 1000000 * 10.0,
            }
        )
        # 最后一天：长下影线（low=9.5，跌5%后收回）+ 收阳 + 缩量（50万 < 前5日均量100万）
        base.loc[29, ["open", "high", "low", "close", "volume"]] = [10.0, 10.15, 9.5, 10.1, 500000]
        df = base.copy()
        result = HuamulanFakeDipStrategy().generate_signals(df)
        assert result.loc[29, "signal"] == 1
        assert result.loc[29, "confidence"] > 0

    def test_no_buy_signal_when_volume_expands(self) -> None:
        """放量下杀不是假摔（原文：放量跌是真出货，不能碰）。"""
        dates = pd.date_range(start="2024-01-01", periods=30, freq="B")
        base = pd.DataFrame(
            {
                "date": dates,
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 1000000,
                "amount": 1000000 * 10.0,
            }
        )
        # 最后一天：长下影线 + 收阳，但放量（200万 > 前5日均量100万）→ 不触发
        base.loc[29, ["open", "high", "low", "close", "volume"]] = [10.0, 10.15, 9.5, 10.1, 2000000]
        result = HuamulanFakeDipStrategy().generate_signals(base)
        assert result.loc[29, "signal"] != 1

    def test_no_buy_signal_when_closes_yin(self) -> None:
        """收阴不是假摔（原文：当天必须收红）。"""
        dates = pd.date_range(start="2024-01-01", periods=30, freq="B")
        base = pd.DataFrame(
            {
                "date": dates,
                "open": 10.0,
                "high": 10.2,
                "low": 9.5,
                "close": 9.6,  # 收阴
                "volume": 500000,  # 缩量
                "amount": 500000 * 10.0,
            }
        )
        result = HuamulanFakeDipStrategy().generate_signals(base)
        assert result.loc[29, "signal"] != 1

    def test_sell_when_breaks_signal_day_low(self) -> None:
        """跌破信号日最低点 → 无条件离场（原文纪律）。"""
        dates = pd.date_range(start="2024-01-01", periods=35, freq="B")
        base = pd.DataFrame(
            {
                "date": dates,
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 1000000,
                "amount": 1000000 * 10.0,
            }
        )
        # 第29天：假摔买入信号（低点9.5）
        base.loc[29, ["open", "high", "low", "close", "volume"]] = [10.0, 10.15, 9.5, 10.1, 500000]
        # 第31天：跌破信号日最低点9.5（低点9.4，收阴大阴线）
        base.loc[31, ["open", "high", "low", "close", "volume"]] = [10.0, 10.05, 9.4, 9.45, 800000]
        result = HuamulanFakeDipStrategy().generate_signals(base)
        assert result.loc[29, "signal"] == 1
        assert result.loc[31, "signal"] == -1

    def test_sell_on_slow_decline_breaks_buy_price(self) -> None:
        """v0.8.0：阴跌场景（不破信号日最低点但跌破买入价5%）也应止损离场。

        修复背景：旧版止损基准 `signal_low × 0.95` 距买入价可达 -13%，
        阴跌行情下 -15.7% 亏损仍不被截断。改为基于信号日收盘价（买入基准）。
        """
        dates = pd.date_range(start="2024-01-01", periods=35, freq="B")
        base = pd.DataFrame(
            {
                "date": dates,
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 1000000,
                "amount": 1000000 * 10.0,
            }
        )
        # 第29天：假摔买入信号（close=10.1，止损基准=10.1×0.95=9.595）
        base.loc[29, ["open", "high", "low", "close", "volume"]] = [10.0, 10.15, 9.5, 10.1, 500000]
        # 第30-33天：阴跌，low 始终 > 9.5（不破信号日最低点），但 close 跌破 9.595
        for i, px in enumerate([9.9, 9.7, 9.6, 9.55]):
            base.loc[30 + i, ["open", "high", "low", "close"]] = [px * 1.005, px * 1.01, 9.52, px]
        result = HuamulanFakeDipStrategy().generate_signals(base)
        assert result.loc[29, "signal"] == 1
        # 第33天 close=9.55 < 9.595 → 止损
        assert result.loc[33, "signal"] == -1

    def test_no_trend_filter(self) -> None:
        """v0.8.0：不要求 MA 多头（原文没有趋势条件），下跌趋势中假摔也可触发。"""
        dates = pd.date_range(start="2024-01-01", periods=30, freq="B")
        # 构造下跌趋势：价格从15跌到10
        prices = np.linspace(15.0, 10.0, 30)
        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices,
                "high": prices * 1.01,
                "low": prices * 0.99,
                "close": prices,
                "volume": 1000000,
                "amount": 1000000 * prices,
            }
        )
        # 最后一天：长下影线（low比close低5%）+ 收阳（close>open）+ 缩量
        df.loc[29, ["open", "high", "low", "volume"]] = [9.8, 10.15, 9.5, 500000]
        df.loc[29, "close"] = 10.05
        df.loc[29, "amount"] = 500000 * 10.05
        result = HuamulanFakeDipStrategy().generate_signals(df)
        # MA5 < MA10 < MA20（下跌趋势），但假摔信号仍应触发
        assert result.loc[29, "signal"] == 1


class TestHuamulanStrategiesIntegration:
    """花木兰策略集成测试。"""

    def test_all_strategies_have_unique_names(
        self,
        tail_pick_strategy: HuamulanTailPickStrategy,
        crash_layout_strategy: HuamulanCrashLayoutStrategy,
        fake_dip_strategy: HuamulanFakeDipStrategy,
    ) -> None:
        names = [
            tail_pick_strategy.name,
            crash_layout_strategy.name,
            fake_dip_strategy.name,
        ]
        assert len(names) == len(set(names))

    def test_all_strategies_have_valid_categories(
        self,
        tail_pick_strategy: HuamulanTailPickStrategy,
        crash_layout_strategy: HuamulanCrashLayoutStrategy,
        fake_dip_strategy: HuamulanFakeDipStrategy,
    ) -> None:
        valid_categories = {"trend", "breakout", "mean_revert", "main_force", "ml"}
        strategies = [
            tail_pick_strategy,
            crash_layout_strategy,
            fake_dip_strategy,
        ]
        for strategy in strategies:
            assert strategy.category in valid_categories

    def test_all_strategies_generate_confidence(
        self,
        tail_pick_strategy: HuamulanTailPickStrategy,
        crash_layout_strategy: HuamulanCrashLayoutStrategy,
        fake_dip_strategy: HuamulanFakeDipStrategy,
        sample_data: pd.DataFrame,
    ) -> None:
        strategies = [
            tail_pick_strategy,
            crash_layout_strategy,
            fake_dip_strategy,
        ]
        for strategy in strategies:
            result = strategy.generate_signals(sample_data)
            assert "confidence" in result.columns
            assert result["confidence"].between(0, 1).all()
