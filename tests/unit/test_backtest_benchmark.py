"""Backtest integration tests using real benchmark CSV data.

These tests load benchmark_600519.csv and run actual backtests
through BacktestEngine, verifying end-to-end behavior.
"""
from __future__ import annotations

import pandas as pd
import pytest

from gugu.backtest import BacktestEngine
from gugu.backtest.engine import BacktestResult, Trade
from gugu.strategies import BollingerStrategy, DualMAStrategy, TurtleStrategy


@pytest.fixture(scope="module")
def benchmark_df() -> pd.DataFrame:
    """Load benchmark data."""
    df = pd.read_csv("tests/fixtures/benchmark_600519.csv")
    df["date"] = pd.to_datetime(df["date"])
    return df


@pytest.fixture(scope="module")
def turtle() -> TurtleStrategy:
    return TurtleStrategy(
        params={"breakout_window": 5, "exit_window": 3, "atr_window": 5}
    )


@pytest.fixture(scope="module")
def dual_ma() -> DualMAStrategy:
    return DualMAStrategy(params={"short_window": 3, "long_window": 5})


@pytest.fixture(scope="module")
def bollinger() -> BollingerStrategy:
    return BollingerStrategy(params={"window": 5, "num_std": 1.0})


@pytest.fixture(scope="module")
def engine() -> BacktestEngine:
    return BacktestEngine(
        initial_capital=1_000_000,
        commission_rate=0.00025,
        position_ratio=0.3,
    )


class TestBacktestWithBenchmark:
    """End-to-end backtests using real benchmark data."""

    def test_backtest_turtle(
        self, benchmark_df: pd.DataFrame, engine: BacktestEngine, turtle: TurtleStrategy,
    ) -> None:
        """Run turtle strategy on benchmark."""
        result = engine.run(turtle, benchmark_df, "600519")

        assert len(result.trades) > 0
        assert len(result.equity_curve) > 0
        assert len(result.equity_curve) == len(benchmark_df)
        assert isinstance(result.metrics["total_return"], float)

        expected_keys = {"total_return", "sharpe", "max_drawdown", "win_rate", "total_trades"}
        assert expected_keys.issubset(result.metrics.keys())

        for t in result.trades:
            if t.direction == "buy":
                assert t.commission > 0
            if t.direction == "sell":
                assert t.stamp_tax > 0

    def test_backtest_dual_ma(
        self, benchmark_df: pd.DataFrame, engine: BacktestEngine, dual_ma: DualMAStrategy,
    ) -> None:
        """Run dual_ma on benchmark."""
        result = engine.run(dual_ma, benchmark_df, "600519")
        assert len(result.trades) >= 1
        assert result.metrics["max_drawdown"] >= 0

    def test_backtest_bollinger(
        self, benchmark_df: pd.DataFrame, engine: BacktestEngine, bollinger: BollingerStrategy,
    ) -> None:
        """Run bollinger on benchmark."""
        result = engine.run(bollinger, benchmark_df, "600519")
        assert len(result.trades) >= 1
        for t in result.trades:
            assert t.direction in ("buy", "sell")

    def test_backtest_with_commission(
        self, benchmark_df: pd.DataFrame, bollinger: BollingerStrategy,
    ) -> None:
        """Higher commission rate should produce lower total return."""
        engine_low = BacktestEngine(
            initial_capital=1_000_000, commission_rate=0.0001, position_ratio=0.3,
        )
        engine_high = BacktestEngine(
            initial_capital=1_000_000, commission_rate=0.001, position_ratio=0.3,
        )
        result_low = engine_low.run(bollinger, benchmark_df, "600519")
        result_high = engine_high.run(bollinger, benchmark_df, "600519")

        assert result_low.metrics["total_return"] != pytest.approx(
            result_high.metrics["total_return"]
        )
        assert result_low.metrics["total_return"] >= result_high.metrics["total_return"]

    def test_backtest_with_different_capital(
        self, benchmark_df: pd.DataFrame, bollinger: BollingerStrategy,
    ) -> None:
        """Total return % should be similar regardless of initial capital."""
        engine_a = BacktestEngine(
            initial_capital=500_000, commission_rate=0.00025, position_ratio=1.0,
        )
        engine_b = BacktestEngine(
            initial_capital=5_000_000, commission_rate=0.00025, position_ratio=1.0,
        )
        result_a = engine_a.run(bollinger, benchmark_df, "600519")
        result_b = engine_b.run(bollinger, benchmark_df, "600519")

        assert len(result_a.trades) == len(result_b.trades)
        assert result_a.metrics["total_return"] == pytest.approx(
            result_b.metrics["total_return"], abs=0.03
        )

    def test_backtest_empty_data(
        self, engine: BacktestEngine, bollinger: BollingerStrategy,
    ) -> None:
        """Empty DataFrame returns empty result gracefully."""
        empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        result = engine.run(bollinger, empty, "600519")
        assert len(result.trades) == 0
        assert len(result.equity_curve) == 0
        assert result.metrics["total_return"] == 0.0
        assert result.metrics["total_trades"] == 0.0

    def test_backtest_result_dataclass(
        self, benchmark_df: pd.DataFrame, engine: BacktestEngine, turtle: TurtleStrategy,
    ) -> None:
        """BacktestResult has all required fields."""
        result = engine.run(turtle, benchmark_df, "600519")
        assert isinstance(result.symbol, str) and result.symbol == "600519"
        assert isinstance(result.strategy_name, str) and result.strategy_name == "turtle"
        assert isinstance(result.trades, list)
        assert isinstance(result.equity_curve, pd.Series)
        assert isinstance(result.metrics, dict)

        if result.trades:
            t = result.trades[0]
            assert isinstance(t, Trade)
            assert hasattr(t, "date") and hasattr(t, "direction")
            assert hasattr(t, "price") and hasattr(t, "quantity")
            assert hasattr(t, "commission") and hasattr(t, "profit")

    def test_backtest_trades_start_with_buy(
        self, benchmark_df: pd.DataFrame, engine: BacktestEngine, turtle: TurtleStrategy,
    ) -> None:
        """First trade should be a buy (策略从空仓开始)。"""
        result = engine.run(turtle, benchmark_df, "600519")
        assert result.trades[0].direction == "buy"

    def test_backtest_has_both_directions(
        self, benchmark_df: pd.DataFrame, engine: BacktestEngine, turtle: TurtleStrategy,
    ) -> None:
        """Trades should include both buys and sells."""
        result = engine.run(turtle, benchmark_df, "600519")
        directions = {t.direction for t in result.trades}
        assert "buy" in directions
        assert "sell" in directions

    def test_backtest_transaction_costs(
        self, benchmark_df: pd.DataFrame, engine: BacktestEngine, bollinger: BollingerStrategy,
    ) -> None:
        """All trades incur non-zero commissions."""
        result = engine.run(bollinger, benchmark_df, "600519")
        for t in result.trades:
            assert t.commission > 0