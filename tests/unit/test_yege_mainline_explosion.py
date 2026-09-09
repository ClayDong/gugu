from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.strategies.yege_mainline_explosion import YegeMainlineExplosionStrategy


def _df(n: int = 80) -> pd.DataFrame:
    close = np.linspace(100.0, 125.0, n)
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n),
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(n, 2_000_000.0),
        }
    )


def test_params_and_output_contract():
    strategy = YegeMainlineExplosionStrategy()
    df = _df()
    out = strategy.generate_signals(df, is_backtest=True)
    assert len(out) == len(df)
    assert set(out["signal"].unique()).issubset({-1, 0, 1})
    assert out["confidence"].between(0, 1).all()
    assert (out["signal"] == 1).sum() == 0


def test_missing_context_is_fail_closed():
    strategy = YegeMainlineExplosionStrategy()
    df = _df()
    df["yege_event_passed"] = True
    df["yege_entry_ready"] = True
    df["yege_evidence_status"] = "unavailable"
    df["yege_candidate"] = True
    df["yege_board_role"] = "space_dragon"
    out = strategy.generate_signals(df, is_backtest=True)
    assert (out["signal"] == 1).sum() == 0


def test_available_context_can_produce_candidate_signal():
    strategy = YegeMainlineExplosionStrategy()
    df = _df()
    df["yege_event_passed"] = True
    df["yege_entry_ready"] = True
    df["yege_evidence_status"] = "available"
    df["yege_candidate"] = True
    df["yege_board_role"] = "space_dragon"
    df["volume"] = np.linspace(1_000_000, 4_000_000, len(df))
    out = strategy.generate_signals(df, is_backtest=True)
    assert set(out["signal"].unique()).issubset({-1, 0, 1})
    assert out["confidence"].max() <= 1


def test_invalid_window_rejected():
    with pytest.raises(ValueError):
        YegeMainlineExplosionStrategy({"first_board_window_min": 2})
