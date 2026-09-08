import numpy as np
import pandas as pd

from gugu.strategies.macd_zero_axis import MacdZeroAxisStrategy


def test_macd_strategy_output_columns_and_bounds():
    close = np.r_[np.linspace(80, 100, 80), np.linspace(100, 92, 40)]
    df = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=len(close)),
            "open": close * 0.998,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(len(close), 1_000_000.0),
            "amount": close * 1_000_000,
        }
    )
    out = MacdZeroAxisStrategy().generate_signals(df, is_backtest=True)
    assert "signal" in out and "confidence" in out and "macd_top_divergence" in out
    assert set(out["signal"].unique()).issubset({-1, 0, 1})
    assert out["confidence"].between(0, 1).all()
