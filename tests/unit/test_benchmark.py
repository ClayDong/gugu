import pytest
import pandas as pd

from gugu.backtest.benchmark import compare_returns, compound_return


def test_compound_return_is_not_simple_sum():
    assert compound_return(pd.Series([0.10, -0.10])) == pytest.approx(-0.01)


def test_compare_returns_aligns_common_dates_and_compounds():
    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    result = compare_returns(
        pd.Series([0.10, 0.0, -0.05], index=idx),
        pd.Series([0.05, 0.0, 0.0], index=idx),
    )
    assert result.common_days == 3
    assert result.strategy_return != result.benchmark_return
    assert result.excess_return < result.strategy_return
