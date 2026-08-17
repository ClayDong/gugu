"""信号路由测试。"""

from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest

from gugu.engine.signal_router import SignalRouter
from gugu.strategies.base import Strategy


class AlwaysBuy(Strategy):
    name = "always_buy"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"] = 1
        df["confidence"] = 0.8
        return df


class AlwaysSell(Strategy):
    name = "always_sell"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"] = -1
        df["confidence"] = 0.8
        return df


class AlwaysHold(Strategy):
    name = "always_hold"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"] = 0
        df["confidence"] = 0.0
        return df


@pytest.fixture
def df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=60, freq="D"),
            "open": [100.0] * 60,
            "high": [101.0] * 60,
            "low": [99.0] * 60,
            "close": [100.0] * 60,
            "volume": [1_000_000] * 60,
            "amount": [100_000_000] * 60,
        }
    )


def test_route_empty_strategies(df: pd.DataFrame) -> None:
    router = SignalRouter([])
    assert router.route(df, "600519") is None


def test_route_too_few_rows() -> None:
    router = SignalRouter([AlwaysBuy()])
    df = pd.DataFrame({"close": [1, 2], "high": [2, 3], "low": [0, 1], "volume": [1, 1]})
    assert router.route(df, "600519") is None


def test_route_any_buy(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.get_config") as mock_cfg:
        mock_cfg.return_value.strategy.signal_fusion = "any"
        mock_cfg.return_value.strategy.min_confidence = 0.5
        router = SignalRouter([AlwaysBuy()])
        sig = router.route(df, "600519", name="茅台")
        assert sig is not None
        assert sig["direction"] == "buy"
        assert sig["name"] == "茅台"
        assert "always_buy" in sig["strategies"]
        assert "always_buy" in sig["strategy"]


def test_route_unanimous_buy(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.get_config") as mock_cfg:
        mock_cfg.return_value.strategy.signal_fusion = "unanimous"
        mock_cfg.return_value.strategy.min_confidence = 0.5
        router = SignalRouter([AlwaysBuy(), AlwaysBuy()])
        sig = router.route(df, "600519")
        assert sig is not None
        assert sig["direction"] == "buy"


def test_route_unanimous_mixed_no_signal(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.get_config") as mock_cfg:
        mock_cfg.return_value.strategy.signal_fusion = "unanimous"
        mock_cfg.return_value.strategy.min_confidence = 0.5
        router = SignalRouter([AlwaysBuy(), AlwaysSell()])
        assert router.route(df, "600519") is None


def test_route_majority_buy(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.get_config") as mock_cfg:
        mock_cfg.return_value.strategy.signal_fusion = "majority"
        mock_cfg.return_value.strategy.min_confidence = 0.5
        router = SignalRouter([AlwaysBuy(), AlwaysBuy(), AlwaysSell()])
        sig = router.route(df, "600519")
        assert sig is not None
        assert sig["direction"] == "buy"


def test_route_majority_no_majority(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.get_config") as mock_cfg:
        mock_cfg.return_value.strategy.signal_fusion = "majority"
        mock_cfg.return_value.strategy.min_confidence = 0.5
        router = SignalRouter([AlwaysBuy(), AlwaysSell()])
        assert router.route(df, "600519") is None


def test_route_low_confidence_filtered(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.get_config") as mock_cfg:
        mock_cfg.return_value.strategy.signal_fusion = "any"
        mock_cfg.return_value.strategy.min_confidence = 0.9
        router = SignalRouter([AlwaysBuy()])
        assert router.route(df, "600519") is None


def test_route_ignores_hold_strategy(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.get_config") as mock_cfg:
        mock_cfg.return_value.strategy.signal_fusion = "any"
        mock_cfg.return_value.strategy.min_confidence = 0.5
        router = SignalRouter([AlwaysHold(), AlwaysBuy()])
        sig = router.route(df, "600519")
        assert sig is not None
        assert sig["direction"] == "buy"


def test_route_volume_confirmed_buy_passes(df: pd.DataFrame) -> None:
    """放量买入信号应通过成交量确认。"""
    with mock.patch("gugu.engine.signal_router.get_config") as mock_cfg:
        mock_cfg.return_value.strategy.signal_fusion = "any"
        mock_cfg.return_value.strategy.min_confidence = 0.5
        df = df.copy()
        df.iloc[-1, df.columns.get_loc("volume")] = 1_500_000
        router = SignalRouter([AlwaysBuy()])
        sig = router.route(df, "600519")
        assert sig is not None
        assert sig["direction"] == "buy"
        assert "volume_confirmed" not in sig


def test_route_low_volume_buy_discount(df: pd.DataFrame) -> None:
    """缩量买入信号置信度应被打折（v0.9.7：0.85 档起，不再一票否决）。"""
    with mock.patch("gugu.engine.signal_router.get_config") as mock_cfg:
        mock_cfg.return_value.strategy.signal_fusion = "any"
        mock_cfg.return_value.strategy.min_confidence = 0.5
        df = df.copy()
        # 使最新量能约为 20 日均量的 0.82 倍，命中 0.95 折扣档
        df.iloc[-1, df.columns.get_loc("volume")] = 800_000
        router = SignalRouter([AlwaysBuy()])
        sig = router.route(df, "600519")
        assert sig is not None
        assert sig["direction"] == "buy"
        assert sig.get("volume_confirmed") is False
        # 0.8 * 0.95 = 0.76（v0.9.7 放宽折扣）
        assert sig["confidence"] == pytest.approx(0.76, abs=0.01)


def test_route_shrink_volume_buy_filtered(df: pd.DataFrame) -> None:
    """极度缩量买入信号置信度应被深度打折但不再直接过滤（v0.9.7 放宽）。"""
    with mock.patch("gugu.engine.signal_router.get_config") as mock_cfg:
        mock_cfg.return_value.strategy.signal_fusion = "any"
        mock_cfg.return_value.strategy.min_confidence = 0.5
        df = df.copy()
        df.iloc[-1, df.columns.get_loc("volume")] = 200_000
        router = SignalRouter([AlwaysBuy()])
        sig = router.route(df, "600519")
        # 0.8 * 0.85 = 0.68 > 0.5 → 信号保留（不再因缩量被直接过滤）
        assert sig is not None
        assert sig.get("volume_confirmed") is False
        assert sig["confidence"] == pytest.approx(0.68, abs=0.01)


def test_route_sell_ignores_volume(df: pd.DataFrame) -> None:
    """卖出信号不受成交量确认影响。"""
    with mock.patch("gugu.engine.signal_router.get_config") as mock_cfg:
        mock_cfg.return_value.strategy.signal_fusion = "any"
        mock_cfg.return_value.strategy.min_confidence = 0.5
        df = df.copy()
        df.iloc[-1, df.columns.get_loc("volume")] = 200_000
        router = SignalRouter([AlwaysSell()])
        sig = router.route(df, "600519")
        assert sig is not None
        assert sig["direction"] == "sell"
        assert "volume_confirmed" not in sig
