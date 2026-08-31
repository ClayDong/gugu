"""回测验证模块的确定性契约测试。"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import pandas as pd

from gugu.backtest.validation import (
    calc_param_perturbation_stability,
    calc_rolling_window_validation,
)
from gugu.strategies import BoxBreakoutStrategy


class _FakeStrategy:
    def __init__(self, params=None):
        self.params = params or {}


class _FakeBacktestEngine:
    def __init__(self, initial_capital):
        self.initial_capital = initial_capital

    def run(self, strategy, df):
        period = strategy.params["period"]
        total_return = 0.4 if period == 10 else 0.1
        return SimpleNamespace(metrics={"total_return": total_return})


class _RollingStrategy:
    def __init__(self, params=None):
        self.params = params or {}


def test_rolling_validation_exposes_non_overlapping_date_boundaries():
    dates = pd.date_range("2024-01-01", "2024-12-31", freq="B")
    df = pd.DataFrame({"date": dates, "close": range(len(dates))})

    class _RollingEngine:
        states = []

        def __init__(self, initial_capital):
            self.initial_capital = initial_capital

        def run(self, strategy, frame, symbol="", initial_state=None):
            self.states.append(initial_state)
            return SimpleNamespace(
                metrics={"total_return": 0.01, "sharpe": 0.2, "total_trades": 1},
                final_state={"cash": 99_000, "position_qty": 100},
            )

    with patch("gugu.backtest.engine.BacktestEngine", _RollingEngine):
        result = calc_rolling_window_validation(
            _RollingStrategy, df, window_months=3, step_months=1
        )

    assert result.windows
    continued_states = [state for state in _RollingEngine.states if state is not None]
    assert len(continued_states) == len(result.windows) - 1
    assert all(state == {"cash": 99_000, "position_qty": 100} for state in continued_states)
    for window in result.windows:
        assert window["train_end"] < window["test_start"]
        assert window["test_start"] <= window["test_end"]
        assert "start" not in window
        assert "end" not in window


def test_rolling_validation_preserves_date_column_for_real_backtest_engine():
    dates = pd.date_range("2024-01-01", periods=260, freq="B")
    close = pd.Series(range(100, 360), dtype=float)
    df = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1_000_000.0,
            "amount": close * 1_000_000,
        }
    )

    result = calc_rolling_window_validation(
        BoxBreakoutStrategy,
        df,
        window_months=3,
        step_months=1,
        param_grid={"box_window": [5], "volume_confirm": [False]},
    )

    assert result.windows


def test_param_stability_always_uses_unperturbed_baseline():
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=20, freq="B")})

    with patch("gugu.backtest.engine.BacktestEngine", _FakeBacktestEngine):
        result = calc_param_perturbation_stability(
            _FakeStrategy,
            df,
            base_params={"period": 10},
            key_params=["period"],
            perturbation_pcts=[-0.1, 0.1],
        )[0]

    assert {item["pct"] for item in result.perturbations} == {-0.1, 0.0, 0.1}
    assert result.base_return == 0.4
    assert result.stability_score == pytest.approx(0.25)
