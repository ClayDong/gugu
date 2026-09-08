from __future__ import annotations

import pandas as pd

from gugu.analysis.market_env import MarketEnvironment


def _df(close: list[float], open_: list[float] | None = None) -> pd.DataFrame:
    open_values = open_ or close
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=len(close)),
            "open": open_values,
            "high": close,
            "low": close,
            "close": close,
            "volume": [1_000_000] * len(close),
        }
    )


def test_market_env_insufficient_is_fail_closed():
    result = MarketEnvironment().assess({"600519": _df([100, 101])})
    assert result["detector_status"] == "insufficient"
    assert result["mode"] == "unavailable"


def test_market_env_ice_point_has_explicit_mode():
    data = {str(i): _df([100, 100, 100]) for i in range(3)}
    result = MarketEnvironment().assess(data)
    assert result["detector_status"] == "available"
    assert result["ice_point"] is True
    assert result["mode"] == "ice_point_buy"
