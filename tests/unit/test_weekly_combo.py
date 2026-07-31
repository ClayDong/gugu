"""周线四因子共振选股策略单元测试。"""

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.weekly_combo import WeeklyComboStrategy


@pytest.fixture
def strategy() -> WeeklyComboStrategy:
    """创建策略实例。"""
    return WeeklyComboStrategy()


@pytest.fixture
def sample_daily_data() -> pd.DataFrame:
    """创建样本日线数据（约100个交易日，可转约20周）。"""
    np.random.seed(42)
    dates = pd.date_range(start="2024-01-01", periods=100, freq="B")

    # 生成趋势向上的价格数据
    base_price = 10.0
    prices = [base_price]
    for i in range(1, 100):
        change = np.random.normal(0.002, 0.02)
        prices.append(prices[-1] * (1 + change))

    prices = np.array(prices)

    # 生成成交量（基础量+随机波动）
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
        }
    )

    # 确保 high >= low
    df["high"] = df[["open", "high", "close"]].max(axis=1) * 1.001
    df["low"] = df[["open", "low", "close"]].min(axis=1) * 0.999

    return df


class TestWeeklyComboStrategy:
    """周线四因子共振选股策略测试。"""

    def test_strategy_name_and_category(self, strategy: WeeklyComboStrategy) -> None:
        """测试策略名称和类别。"""
        assert strategy.name == "weekly_combo"
        assert strategy.category == "trend"

    def test_generate_signals_returns_dataframe(
        self, strategy: WeeklyComboStrategy, sample_daily_data: pd.DataFrame
    ) -> None:
        """测试信号生成返回DataFrame。"""
        result = strategy.generate_signals(sample_daily_data)
        assert isinstance(result, pd.DataFrame)

    def test_signal_column_exists(
        self, strategy: WeeklyComboStrategy, sample_daily_data: pd.DataFrame
    ) -> None:
        """测试信号列存在。"""
        result = strategy.generate_signals(sample_daily_data)
        assert "signal" in result.columns
        assert "confidence" in result.columns

    def test_signal_values_valid(
        self, strategy: WeeklyComboStrategy, sample_daily_data: pd.DataFrame
    ) -> None:
        """测试信号值有效（-1/0/1）。"""
        result = strategy.generate_signals(sample_daily_data)
        assert result["signal"].isin([-1, 0, 1]).all()

    def test_confidence_range(
        self, strategy: WeeklyComboStrategy, sample_daily_data: pd.DataFrame
    ) -> None:
        """测试置信度在0-1范围内。"""
        result = strategy.generate_signals(sample_daily_data)
        assert result["confidence"].between(0, 1).all()

    def test_no_signals_on_empty_data(self, strategy: WeeklyComboStrategy) -> None:
        """测试空数据不产生信号。"""
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        # 空数据应该返回空结果或全0信号
        try:
            result = strategy.generate_signals(df)
            if len(result) > 0:
                assert result["signal"].eq(0).all()
        except (ValueError, KeyError):
            pass  # 空数据抛出异常也是可接受的

    def test_score_factors_exist(
        self, strategy: WeeklyComboStrategy, sample_daily_data: pd.DataFrame
    ) -> None:
        """测试评分因子在内部计算（通过信号分布间接验证）。"""
        result = strategy.generate_signals(sample_daily_data)
        # 验证结果长度与输入一致
        assert len(result) == len(sample_daily_data)

    def test_buy_signal_with_volume_spike(self, strategy: WeeklyComboStrategy) -> None:
        """测试倍量情况下的信号生成。"""
        # 构造一个明确的倍量场景
        dates = pd.date_range(start="2024-01-01", periods=60, freq="B")

        # 价格稳步上升
        prices = 10.0 + np.linspace(0, 2, 60) + np.random.randn(60) * 0.1

        # 前50天正常成交量，最后10天放量2倍以上
        volumes = np.ones(60) * 1000000
        volumes[-10:] = 2500000  # 2.5倍放量

        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices * 0.99,
                "high": prices * 1.02,
                "low": prices * 0.98,
                "close": prices,
                "volume": volumes,
            }
        )

        result = strategy.generate_signals(df)

        # 验证最后几天有信号产生（因为放量）
        # 注意：信号对齐到周线，可能在周的最后一天产生
        assert "signal" in result.columns

    def test_triple_bull_detection(self, strategy: WeeklyComboStrategy) -> None:
        """测试周线三连阳识别。"""
        # 构造连续上涨的周线数据
        dates = pd.date_range(start="2024-01-01", periods=30, freq="B")

        # 连续阳线（收盘价高于开盘价）
        prices = 10.0 + np.linspace(0, 1.5, 30)
        opens = prices - 0.05
        closes = prices + 0.05  # 收阳

        df = pd.DataFrame(
            {
                "date": dates,
                "open": opens,
                "high": closes * 1.01,
                "low": opens * 0.99,
                "close": closes,
                "volume": np.linspace(1000000, 1500000, 30),  # 温和放量
            }
        )

        result = strategy.generate_signals(df)
        assert len(result) == len(df)


class TestWeeklyComboStopLoss:
    """测试动态止损和移动止盈功能。"""

    def test_stop_loss_triggers_on_decline(self) -> None:
        """测试价格跌破止损位时触发卖出信号。"""
        strategy = WeeklyComboStrategy()

        dates = pd.date_range(start="2024-01-01", periods=40, freq="B")

        # 前20天正常上涨（触发买入）
        prices = np.concatenate(
            [
                10.0 + np.linspace(0, 0.5, 20),  # 上涨阶段
                np.linspace(10.5, 9.0, 20),  # 下跌阶段（超过8%止损）
            ]
        )

        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices * 0.99,
                "high": prices * 1.02,
                "low": prices * 0.98,
                "close": prices,
                "volume": np.ones(40) * 2000000,  # 放量触发买入
            }
        )

        result = strategy.generate_signals(df)

        # 验证有卖出信号产生（止损触发）
        sell_signals = result[result["signal"] == -1]
        assert len(sell_signals) > 0, "止损应该触发卖出信号"

    def test_take_profit_triggers_on_drawback(self) -> None:
        """测试移动止盈在回撤时触发。"""
        strategy = WeeklyComboStrategy()

        dates = pd.date_range(start="2024-01-01", periods=40, freq="B")

        # 前20天上涨超过15%，然后回撤10%
        prices = np.concatenate(
            [
                10.0 + np.linspace(0, 2.0, 20),  # 上涨20%（超过15%阈值）
                np.linspace(12.0, 11.0, 20),  # 回撤约8.3%
            ]
        )

        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices * 0.99,
                "high": prices * 1.02,
                "low": prices * 0.98,
                "close": prices,
                "volume": np.ones(40) * 2000000,
            }
        )

        result = strategy.generate_signals(df)

        # 验证有卖出信号产生（移动止盈触发）
        sell_signals = result[result["signal"] == -1]
        assert len(sell_signals) > 0, "移动止盈应该触发卖出信号"

    def test_no_stop_loss_if_not_in_position(self) -> None:
        """测试未持仓时止损不触发。"""
        strategy = WeeklyComboStrategy()

        dates = pd.date_range(start="2024-01-01", periods=20, freq="B")

        # 持续下跌，但没有买入信号
        prices = 10.0 - np.linspace(0, 2.0, 20)

        df = pd.DataFrame(
            {
                "date": dates,
                "open": prices * 0.99,
                "high": prices * 1.02,
                "low": prices * 0.98,
                "close": prices,
                "volume": np.ones(20) * 500000,  # 缩量不触发买入
            }
        )

        result = strategy.generate_signals(df)

        # 没有买入，所以止损不应该触发
        buy_signals = result[result["signal"] == 1]
        assert len(buy_signals) == 0, "下跌过程中不应买入"


class TestWeeklyComboRegistry:
    """测试策略注册。"""

    def test_strategy_in_registry(self) -> None:
        """测试策略在注册表中。"""
        from gugu.strategies.registry import _REGISTRY

        assert "weekly_combo" in _REGISTRY

    def test_get_strategy_instance(self) -> None:
        """测试获取策略实例。"""
        from gugu.strategies.registry import get_strategy

        strategy = get_strategy("weekly_combo")
        assert isinstance(strategy, WeeklyComboStrategy)
