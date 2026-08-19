"""回测验证模块的确定性契约测试。"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import pandas as pd

from gugu.backtest.validation import (
    calc_param_perturbation_stability,
    calc_rolling_window_validation,
)


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
        def __init__(self, initial_capital):
            self.initial_capital = initial_capital

        def run(self, strategy, frame):
            return SimpleNamespace(metrics={"total_return": 0.01, "sharpe": 0.2, "total_trades": 1})

    with patch("gugu.backtest.engine.BacktestEngine", _RollingEngine):
        result = calc_rolling_window_validation(
            _RollingStrategy, df, window_months=3, step_months=1
        )

    assert result.windows
    for window in result.windows:
        assert window["train_end"] < window["test_start"]
        assert window["test_start"] <= window["test_end"]
        assert "start" not in window
        assert "end" not in window


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
