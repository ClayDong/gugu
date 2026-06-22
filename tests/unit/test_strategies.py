"""策略基类与内置策略测试。"""
from __future__ import annotations

import pandas as pd
import pytest

from gugu.strategies.base import Strategy, StrategyConfig
from gugu.strategies.breakout import BoxBreakoutStrategy, DualThrustStrategy
from gugu.strategies.mean_revert import BollingerStrategy, KDJStrategy, RSIStrategy
from gugu.strategies.registry import get_strategy, list_strategies
from gugu.strategies.trend import DualMAStrategy, MACDStrategy, TurtleStrategy


def test_strategy_config_defaults() -> None:
    cfg = StrategyConfig(name="demo", params={"a": 1})
    assert cfg.enabled is True


def test_base_ensure_columns_missing(ohlcv_df: pd.DataFrame) -> None:
    class DummyStrategy(Strategy):
        name = "dummy"

        def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
            self._ensure_columns(df)
            return df

    df = ohlcv_df.drop(columns=["close"])
    with pytest.raises(ValueError, match="缺失列"):
        DummyStrategy().generate_signals(df)


def test_turtle_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = TurtleStrategy(
        {"breakout_window": 20, "exit_window": 10, "atr_window": 20}
    )
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns
    assert set(out["signal"].unique()).issubset({-1, 0, 1})


def test_dual_ma_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = DualMAStrategy({"short_window": 5, "long_window": 20})
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_macd_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = MACDStrategy(
        {"short_window": 12, "long_window": 26, "signal_window": 9}
    )
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_bollinger_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = BollingerStrategy({"window": 20, "num_std": 2.0})
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_rsi_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = RSIStrategy(
        {"window": 14, "oversold": 30, "overbought": 70}
    )
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_kdj_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = KDJStrategy(
        {"window": 9, "oversold": 20, "overbought": 80}
    )
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_box_breakout_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = BoxBreakoutStrategy(
        {"box_window": 20, "volume_confirm": True, "volume_ratio": 1.5}
    )
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_dual_thrust_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = DualThrustStrategy(
        {"lookback": 5, "k_up": 0.5, "k_down": 0.5}
    )
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_registry_get_strategy() -> None:
    assert "turtle" in list_strategies()
    strat = get_strategy("turtle")
    assert isinstance(strat, TurtleStrategy)


def test_registry_unknown_strategy() -> None:
    with pytest.raises(KeyError):
        get_strategy("not_exist")
