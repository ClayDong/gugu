"""斌哥三个策略的单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.binge_714 import Binge714Strategy
from gugu.strategies.binge_trend import BingeTrendStrategy
from gugu.strategies.binge_wave import BingeWaveStrategy


def _df(n: int = 120, base: float = 100.0, trend: float = 0.002) -> pd.DataFrame:
    """生成带趋势和波动的合成K线。"""
    rng = np.random.RandomState(42)
    close = base * np.cumprod(1 + trend + rng.randn(n) * 0.02)
    close = np.maximum(close, 5.0)
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n),
            "open": close * (1 - rng.rand(n) * 0.01),
            "high": close * (1 + rng.rand(n) * 0.02),
            "low": close * (1 - rng.rand(n) * 0.02),
            "close": close,
            "volume": rng.randint(1_000_000, 5_000_000, size=n).astype(float),
            "amount": close * rng.randint(1_000_000, 5_000_000, size=n).astype(float),
        }
    )


class TestBinge714:
    def test_output_contract(self):
        df = _df()
        out = Binge714Strategy().generate_signals(df, is_backtest=True)
        assert len(out) == len(df)
        assert set(out["signal"].unique()).issubset({-1, 0, 1})
        assert out["confidence"].between(0, 1).all()

    def test_invalid_params_rejected(self):
        with pytest.raises(ValueError):
            Binge714Strategy({"consecutive_yang_min": 2})
        with pytest.raises(ValueError):
            Binge714Strategy({"trailing_stop_pct": 0})


class TestBingeWave:
    def test_output_contract(self):
        df = _df()
        out = BingeWaveStrategy().generate_signals(df, is_backtest=True)
        assert len(out) == len(df)
        assert set(out["signal"].unique()).issubset({-1, 0, 1})
        assert out["confidence"].between(0, 1).all()

    def test_invalid_params_rejected(self):
        with pytest.raises(ValueError):
            BingeWaveStrategy({"ema_period": 0})
        with pytest.raises(ValueError):
            BingeWaveStrategy({"vol_ratio_min": 0.5})


class TestBingeTrend:
    def test_output_contract(self):
        df = _df()
        out = BingeTrendStrategy().generate_signals(df, is_backtest=True)
        assert len(out) == len(df)
        assert set(out["signal"].unique()).issubset({-1, 0, 1})
        assert out["confidence"].between(0, 1).all()

    def test_invalid_params_rejected(self):
        with pytest.raises(ValueError):
            BingeTrendStrategy({"ema_short": 0})
        with pytest.raises(ValueError):
            BingeTrendStrategy({"ema_mid": 5})
        with pytest.raises(ValueError):
            BingeTrendStrategy({"ema_long": 15})
