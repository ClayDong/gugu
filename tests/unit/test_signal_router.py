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
    with mock.patch("gugu.engine.signal_router.settings") as mock_settings:
        mock_settings.return_value = {"strategy": {"signal_fusion": "any", "min_confidence": 0.5}}
        router = SignalRouter([AlwaysBuy()])
        sig = router.route(df, "600519")
        assert sig is not None
        assert sig["direction"] == "buy"
        assert "always_buy" in sig["strategies"]


def test_route_unanimous_buy(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.settings") as mock_settings:
        mock_settings.return_value = {
            "strategy": {"signal_fusion": "unanimous", "min_confidence": 0.5}
        }
        router = SignalRouter([AlwaysBuy(), AlwaysBuy()])
        sig = router.route(df, "600519")
        assert sig is not None
        assert sig["direction"] == "buy"


def test_route_unanimous_mixed_no_signal(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.settings") as mock_settings:
        mock_settings.return_value = {
            "strategy": {"signal_fusion": "unanimous", "min_confidence": 0.5}
        }
        router = SignalRouter([AlwaysBuy(), AlwaysSell()])
        assert router.route(df, "600519") is None


def test_route_majority_buy(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.settings") as mock_settings:
        mock_settings.return_value = {
            "strategy": {"signal_fusion": "majority", "min_confidence": 0.5}
        }
        router = SignalRouter([AlwaysBuy(), AlwaysBuy(), AlwaysSell()])
        sig = router.route(df, "600519")
        assert sig is not None
        assert sig["direction"] == "buy"


def test_route_majority_no_majority(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.settings") as mock_settings:
        mock_settings.return_value = {
            "strategy": {"signal_fusion": "majority", "min_confidence": 0.5}
        }
        router = SignalRouter([AlwaysBuy(), AlwaysSell()])
        assert router.route(df, "600519") is None


def test_route_low_confidence_filtered(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.settings") as mock_settings:
        mock_settings.return_value = {
            "strategy": {"signal_fusion": "any", "min_confidence": 0.9}
        }
        router = SignalRouter([AlwaysBuy()])
        assert router.route(df, "600519") is None


def test_route_ignores_hold_strategy(df: pd.DataFrame) -> None:
    with mock.patch("gugu.engine.signal_router.settings") as mock_settings:
        mock_settings.return_value = {"strategy": {"signal_fusion": "any", "min_confidence": 0.5}}
        router = SignalRouter([AlwaysHold(), AlwaysBuy()])
        sig = router.route(df, "600519")
        assert sig is not None
        assert sig["direction"] == "buy"
