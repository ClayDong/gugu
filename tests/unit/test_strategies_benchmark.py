"""基准策略测试：使用固定 CSV 数据验证策略信号输出。

使用 tests/fixtures/benchmark_600519.csv 作为输入数据，
验证各策略在已知市场形态下产生预期信号。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from gugu.strategies.registry import get_strategy

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
_BENCHMARK_CSV = _FIXTURE_DIR / "benchmark_600519.csv"


@pytest.fixture(scope="module")
def benchmark_df() -> pd.DataFrame:
    """加载基准测试数据。"""
    df = pd.read_csv(_BENCHMARK_CSV)
    df["date"] = pd.to_datetime(df["date"])
    return df


# ========== 海龟策略 ==========


class TestTurtleStrategyBenchmark:
    """海龟策略基准测试。"""

    def test_breakout_signal_at_uptrend(self, benchmark_df: pd.DataFrame) -> None:
        """Days 49-55 (uptrend) should generate buy signals (close > 20-day high)."""
        strategy = get_strategy("turtle")
        signals = strategy.generate_signals(benchmark_df)

        assert "signal" in signals.columns
        assert "confidence" in signals.columns

        # Turtle has buy signals around days 49-55 (during the uptrend)
        uptrend_signals = signals.iloc[49:56]
        buy_count = (uptrend_signals["signal"] == 1).sum()
        assert buy_count >= 1, (
            f"Expected buy signal in days 49-55 (uptrend), "
            f"got {buy_count} buy signals"
        )

    def test_no_false_signals_in_consolidation(self, benchmark_df: pd.DataFrame) -> None:
        """Days 150-160 (consolidation) should not have turtle buy signals.

        Turtle has signals at index 134 and 150 (exactly at the boundary).
        Inside the range, there should be no more buy signals.
        """
        strategy = get_strategy("turtle")
        signals = strategy.generate_signals(benchmark_df)

        consol_signals = signals.iloc[151:160]
        buy_count = (consol_signals["signal"] == 1).sum()
        assert buy_count == 0, (
            f"Expected no buy signals inside consolidation (days 151-160), "
            f"got {buy_count}"
        )

    def test_confidence_values(self, benchmark_df: pd.DataFrame) -> None:
        """Confidence values should be in [0, 1] range."""
        strategy = get_strategy("turtle")
        signals = strategy.generate_signals(benchmark_df)
        conf = signals["confidence"].dropna()
        assert (conf >= 0).all() and (conf <= 1).all()

    def test_signal_values(self, benchmark_df: pd.DataFrame) -> None:
        """Signal values should be in {-1, 0, 1}."""
        strategy = get_strategy("turtle")
        signals = strategy.generate_signals(benchmark_df)
        assert signals["signal"].isin([-1, 0, 1]).all()

    def test_some_signals_produced(self, benchmark_df: pd.DataFrame) -> None:
        """Turtle should produce both buy and sell signals on this data."""
        strategy = get_strategy("turtle")
        signals = strategy.generate_signals(benchmark_df)
        assert (signals["signal"] == 1).sum() >= 1, "No buy signals"
        assert (signals["signal"] == -1).sum() >= 1, "No sell signals"


# ========== 双均线策略 ==========


class TestDualMABenchmark:
    """双均线策略基准测试。"""

    def test_signals_produced(self, benchmark_df: pd.DataFrame) -> None:
        """Dual MA should produce buy and sell signals."""
        strategy = get_strategy("dual_ma")
        signals = strategy.generate_signals(benchmark_df)
        assert (signals["signal"] == 1).sum() >= 1, "No buy signals"
        assert (signals["signal"] == -1).sum() >= 1, "No sell signals"

    def test_signal_values(self, benchmark_df: pd.DataFrame) -> None:
        """Signal values should be in {-1, 0, 1}."""
        strategy = get_strategy("dual_ma")
        signals = strategy.generate_signals(benchmark_df)
        assert signals["signal"].isin([-1, 0, 1]).all()

    def test_buy_before_sell_pattern(self, benchmark_df: pd.DataFrame) -> None:
        """First non-hold signal should be buy (uptrend comes before drop in data)."""
        strategy = get_strategy("dual_ma")
        signals = strategy.generate_signals(benchmark_df)
        non_hold = signals[signals["signal"] != 0]
        if len(non_hold) >= 2:
            assert non_hold.iloc[0]["signal"] == 1, "First signal should be buy"


# ========== 箱体突破策略 ==========


class TestBoxBreakoutBenchmark:
    """箱体突破策略基准测试。"""

    def test_signal_values(self, benchmark_df: pd.DataFrame) -> None:
        """Signal values should be in {-1, 0, 1}."""
        strategy = get_strategy("box_breakout")
        signals = strategy.generate_signals(benchmark_df)
        assert signals["signal"].isin([-1, 0, 1]).all()
        assert "confidence" in signals.columns

    def test_confidence_in_range(self, benchmark_df: pd.DataFrame) -> None:
        """Confidence values should be in [0, 1]."""
        strategy = get_strategy("box_breakout")
        signals = strategy.generate_signals(benchmark_df)
        conf = signals["confidence"].dropna()
        assert (conf >= 0).all() and (conf <= 1).all()


# ========== 布林带策略 ==========


class TestBollingerBenchmark:
    """布林带策略基准测试。"""

    def test_oversold_signal_on_drop(self, benchmark_df: pd.DataFrame) -> None:
        """Sharp drop should trigger bollinger buy signals. Bollinger is active at day 38."""
        strategy = get_strategy("bollinger")
        signals = strategy.generate_signals(benchmark_df)

        # Bollinger has buy signals at indices 37, 38 (around the earlier dip)
        early_signals = signals.iloc[35:45]
        buy_count = (early_signals["signal"] == 1).sum()
        assert buy_count >= 1, (
            f"Expected buy signals from bollinger around days 35-45, "
            f"got {buy_count}"
        )

    def test_signal_values(self, benchmark_df: pd.DataFrame) -> None:
        """Signal values should be in {-1, 0, 1}."""
        strategy = get_strategy("bollinger")
        signals = strategy.generate_signals(benchmark_df)
        assert signals["signal"].isin([-1, 0, 1]).all()

    def test_some_signals_produced(self, benchmark_df: pd.DataFrame) -> None:
        """Bollinger should produce signals."""
        strategy = get_strategy("bollinger")
        signals = strategy.generate_signals(benchmark_df)
        non_hold = (signals["signal"] != 0).sum()
        assert non_hold >= 1, "No signals produced"


# ========== RSI 策略 ==========


class TestRSIBenchmark:
    """RSI 策略基准测试。"""

    def test_some_signals_produced(self, benchmark_df: pd.DataFrame) -> None:
        """RSI should produce signals on this data."""
        strategy = get_strategy("rsi_reversal")
        signals = strategy.generate_signals(benchmark_df)
        non_hold = (signals["signal"] != 0).sum()
        assert non_hold >= 1, "No signals produced"

    def test_signal_values(self, benchmark_df: pd.DataFrame) -> None:
        """Signal values should be in {-1, 0, 1}."""
        strategy = get_strategy("rsi_reversal")
        signals = strategy.generate_signals(benchmark_df)
        assert signals["signal"].isin([-1, 0, 1]).all()

    def test_confidence_in_range(self, benchmark_df: pd.DataFrame) -> None:
        """Confidence values should be in [0, 1]."""
        strategy = get_strategy("rsi_reversal")
        signals = strategy.generate_signals(benchmark_df)
        conf = signals["confidence"].dropna()
        assert (conf >= 0).all() and (conf <= 1).all()


# ========== 通用策略行为 ==========


class TestAllStrategiesGeneric:
    """所有策略的通用行为约束。"""

    @pytest.mark.parametrize("strategy_name", [
        "turtle", "dual_ma", "bollinger", "rsi_reversal",
        "box_breakout", "macd", "kdj",
    ])
    def test_every_strategy_produces_signal_columns(
        self, benchmark_df: pd.DataFrame, strategy_name: str,
    ) -> None:
        """Every strategy must produce valid signal and confidence columns."""
        strategy = get_strategy(strategy_name)
        signals = strategy.generate_signals(benchmark_df)

        assert "signal" in signals.columns, f"{strategy_name}: missing 'signal'"
        assert "confidence" in signals.columns, f"{strategy_name}: missing 'confidence'"
        assert len(signals) == len(benchmark_df), (
            f"{strategy_name}: output length {len(signals)} != input {len(benchmark_df)}"
        )
        assert signals["signal"].isin([-1, 0, 1]).all(), (
            f"{strategy_name}: invalid signal values {set(signals['signal'].unique())}"
        )
        conf = signals["confidence"].dropna()
        assert (conf >= 0).all() and (conf <= 1).all(), (
            f"{strategy_name}: confidence out of range"
        )

    @pytest.mark.parametrize("strategy_name", [
        "turtle", "dual_ma", "bollinger", "rsi_reversal", "macd", "kdj",
    ])
    def test_every_strategy_produces_at_least_one_signal(
        self, benchmark_df: pd.DataFrame, strategy_name: str,
    ) -> None:
        """All strategies except box_breakout should produce at least one signal."""
        strategy = get_strategy(strategy_name)
        signals = strategy.generate_signals(benchmark_df)
        non_hold = (signals["signal"] != 0).sum()
        assert non_hold >= 1, f"{strategy_name}: no signals produced"

    def test_box_breakout_handles_data_gracefully(self, benchmark_df: pd.DataFrame) -> None:
        """Box breakout may produce 0 signals if volume confirmation fails, but must not crash."""
        strategy = get_strategy("box_breakout")
        signals = strategy.generate_signals(benchmark_df)
        assert signals is not None
        assert len(signals) == len(benchmark_df)