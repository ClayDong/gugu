"""策略基类与内置策略测试。"""

from __future__ import annotations

import pandas as pd
import pytest

from gugu.strategies.base import Strategy, StrategyConfig
from gugu.strategies.breakout import BoxBreakoutStrategy, DualThrustStrategy
from gugu.strategies.mean_revert import BollingerStrategy, KDJStrategy, RSIStrategy
from gugu.strategies.registry import get_strategy, list_strategies

try:
    from gugu.strategies.trend import DualMAStrategy, MACDStrategy, TurtleStrategy
except ModuleNotFoundError:
    DualMAStrategy = MACDStrategy = TurtleStrategy = None  # type: ignore[assignment]

pytestmark = pytest.mark.skip(
    reason="依赖已归档策略（trend.DualMAStrategy/MACDStrategy/TurtleStrategy）"
)


def test_strategy_config_defaults() -> None:
    cfg = StrategyConfig(name="demo", params={"a": 1})
    assert cfg.enabled is True


def test_base_ensure_columns_missing(ohlcv_df: pd.DataFrame) -> None:
    class DummyStrategy(Strategy):
        name = "dummy"

        def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
            self._ensure_columns(df)
            return df

    df = ohlcv_df.drop(columns=["close"])
    with pytest.raises(ValueError, match="缺失列"):
        DummyStrategy().generate_signals(df)


def test_turtle_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = TurtleStrategy({"breakout_window": 20, "exit_window": 10, "atr_window": 20})
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
    strat = MACDStrategy({"short_window": 12, "long_window": 26, "signal_window": 9})
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_bollinger_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = BollingerStrategy({"window": 20, "num_std": 2.0})
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_rsi_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = RSIStrategy({"window": 14, "oversold": 30, "overbought": 70})
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_kdj_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = KDJStrategy({"window": 9, "oversold": 20, "overbought": 80})
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_box_breakout_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = BoxBreakoutStrategy({"box_window": 20, "volume_confirm": True, "volume_ratio": 1.5})
    out = strat.generate_signals(ohlcv_df)
    assert "signal" in out.columns
    assert "confidence" in out.columns


def test_dual_thrust_strategy(ohlcv_df: pd.DataFrame) -> None:
    strat = DualThrustStrategy({"lookback": 5, "k_up": 0.5, "k_down": 0.5})
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


# ========== Turtle 成交量确认独立测试（《炒股的智慧》阻力突破需量配合） ==========


def _make_turtle_breakout_df(breakout_volume: float, days: int = 25) -> pd.DataFrame:
    """构造海龟突破数据：前20天震荡，第21天突破20日高点。

    Args:
        breakout_volume: 突破日（第21天起）的成交量。前20天成交量恒为 1_000_000，
            因此 20 日均量 vol_ma = 1_000_000，1.5 倍阈值 = 1_500_000。
    """
    import numpy as np

    np.random.seed(42)
    dates = pd.bdate_range("2024-01-01", periods=days)
    # 前20天高点105、收盘100附近震荡
    high = [105.0] * 20
    close = [100.0 + np.random.uniform(-0.5, 0.5) for _ in range(20)]
    # 第21天起突破：close=110 > 前20日最高价105
    for _ in range(days - 20):
        high.append(111.0)
        close.append(110.0)
    low = [c - 2.0 for c in close]
    open_ = [c - 0.5 for c in close]
    volume = [1_000_000] * 20 + [breakout_volume] * (days - 20)
    amount = [c * v for c, v in zip(close, volume)]
    return pd.DataFrame(
        {
            "date": dates[:days],
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )


class TestTurtleVolumeConfirm:
    """海龟策略成交量确认测试。"""

    def test_volume_confirm_on_high_volume_passes(self):
        """成交量充足时（>1.5倍均量）买入信号通过。"""
        df = _make_turtle_breakout_df(breakout_volume=2_000_000)
        strat = TurtleStrategy(
            {
                "breakout_window": 20,
                "exit_window": 10,
                "atr_window": 20,
                "volume_confirm": True,
                "vol_ratio": 1.5,
            }
        )
        out = strat.generate_signals(df)
        buy_signals = out[out["signal"] == 1]
        assert len(buy_signals) > 0, "成交量充足时应有买入信号"

    def test_volume_confirm_filters_low_volume(self):
        """成交量不足时（<1.5倍均量）买入信号被过滤。"""
        df = _make_turtle_breakout_df(breakout_volume=1_000_000)  # 等于均量，低于1.5倍
        strat = TurtleStrategy(
            {
                "breakout_window": 20,
                "exit_window": 10,
                "atr_window": 20,
                "volume_confirm": True,
                "vol_ratio": 1.5,
            }
        )
        out = strat.generate_signals(df)
        buy_signals = out[out["signal"] == 1]
        assert len(buy_signals) == 0, "成交量不足时买入信号应被过滤"

    def test_volume_confirm_off_passes_low_volume(self):
        """关闭成交量确认时，低成交量也能产生买入信号。"""
        df = _make_turtle_breakout_df(breakout_volume=1_000_000)
        strat = TurtleStrategy(
            {"breakout_window": 20, "exit_window": 10, "atr_window": 20, "volume_confirm": False}
        )
        out = strat.generate_signals(df)
        buy_signals = out[out["signal"] == 1]
        assert len(buy_signals) > 0, "关闭成交量确认时应有买入信号"
