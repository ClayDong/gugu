"""花木兰策略系列单元测试。"""

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.huamulan_tail_pick import HuamulanTailPickStrategy
from gugu.strategies.huamulan_crash_layout import HuamulanCrashLayoutStrategy
from gugu.strategies.huamulan_fake_dip import HuamulanFakeDipStrategy
from gugu.strategies.huamulan_ghost_breakout import HuamulanGhostBreakoutStrategy


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
def ghost_breakout_strategy() -> HuamulanGhostBreakoutStrategy:
    return HuamulanGhostBreakoutStrategy()


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


class TestHuamulanGhostBreakoutStrategy:
    """花木兰烂板成妖打板战法测试。"""

    def test_strategy_name_and_category(
        self, ghost_breakout_strategy: HuamulanGhostBreakoutStrategy
    ) -> None:
        assert ghost_breakout_strategy.name == "huamulan_ghost_breakout"
        assert ghost_breakout_strategy.category == "main_force"

    def test_generate_signals_returns_dataframe(
        self, ghost_breakout_strategy: HuamulanGhostBreakoutStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = ghost_breakout_strategy.generate_signals(sample_data)
        assert isinstance(result, pd.DataFrame)

    def test_signal_column_exists(
        self, ghost_breakout_strategy: HuamulanGhostBreakoutStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = ghost_breakout_strategy.generate_signals(sample_data)
        assert "signal" in result.columns
        assert "confidence" in result.columns

    def test_signal_values_valid(
        self, ghost_breakout_strategy: HuamulanGhostBreakoutStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = ghost_breakout_strategy.generate_signals(sample_data)
        assert result["signal"].isin([-1, 0, 1]).all()

    def test_confidence_range(
        self, ghost_breakout_strategy: HuamulanGhostBreakoutStrategy, sample_data: pd.DataFrame
    ) -> None:
        result = ghost_breakout_strategy.generate_signals(sample_data)
        assert result["confidence"].between(0, 1).all()

    def test_validate_params_valid(
        self, ghost_breakout_strategy: HuamulanGhostBreakoutStrategy
    ) -> None:
        ghost_breakout_strategy.validate_params()

    def test_validate_params_invalid_limit_up(self) -> None:
        with pytest.raises(ValueError, match="limit_up_pct 应在 5%-11% 之间"):
            HuamulanGhostBreakoutStrategy({"limit_up_pct": 0.20})

    def test_validate_params_invalid_vol_expand(self) -> None:
        with pytest.raises(ValueError, match="vol_expand_ratio 必须 >= 1.0"):
            HuamulanGhostBreakoutStrategy({"vol_expand_ratio": 0.5})


class TestHuamulanStrategiesIntegration:
    """花木兰策略集成测试。"""

    def test_all_strategies_have_unique_names(
        self,
        tail_pick_strategy: HuamulanTailPickStrategy,
        crash_layout_strategy: HuamulanCrashLayoutStrategy,
        fake_dip_strategy: HuamulanFakeDipStrategy,
        ghost_breakout_strategy: HuamulanGhostBreakoutStrategy,
    ) -> None:
        names = [
            tail_pick_strategy.name,
            crash_layout_strategy.name,
            fake_dip_strategy.name,
            ghost_breakout_strategy.name,
        ]
        assert len(names) == len(set(names))

    def test_all_strategies_have_valid_categories(
        self,
        tail_pick_strategy: HuamulanTailPickStrategy,
        crash_layout_strategy: HuamulanCrashLayoutStrategy,
        fake_dip_strategy: HuamulanFakeDipStrategy,
        ghost_breakout_strategy: HuamulanGhostBreakoutStrategy,
    ) -> None:
        valid_categories = {"trend", "breakout", "mean_revert", "main_force", "ml"}
        strategies = [
            tail_pick_strategy,
            crash_layout_strategy,
            fake_dip_strategy,
            ghost_breakout_strategy,
        ]
        for strategy in strategies:
            assert strategy.category in valid_categories

    def test_all_strategies_generate_confidence(
        self,
        tail_pick_strategy: HuamulanTailPickStrategy,
        crash_layout_strategy: HuamulanCrashLayoutStrategy,
        fake_dip_strategy: HuamulanFakeDipStrategy,
        ghost_breakout_strategy: HuamulanGhostBreakoutStrategy,
        sample_data: pd.DataFrame,
    ) -> None:
        strategies = [
            tail_pick_strategy,
            crash_layout_strategy,
            fake_dip_strategy,
            ghost_breakout_strategy,
        ]
        for strategy in strategies:
            result = strategy.generate_signals(sample_data)
            assert "confidence" in result.columns
            assert result["confidence"].between(0, 1).all()
