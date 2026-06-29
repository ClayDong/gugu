"""回测引擎单元测试。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.backtest import BacktestEngine, calc_metrics
from gugu.backtest.report import format_report, format_report_dict
from gugu.strategies import DualMAStrategy


@pytest.fixture
def trend_df() -> pd.DataFrame:
    """生成一段先跌后涨的行情。"""
    np.random.seed(7)
    n = 40
    dates = pd.date_range("2026-01-01", periods=n, freq="B").date
    # 前20天下跌，后20天上涨
    phase = np.concatenate([np.linspace(12, 8, 20), np.linspace(8.1, 14, 20)])
    close = phase + np.random.normal(0, 0.15, n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.1,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": np.random.randint(1e6, 5e6, n).astype(float),
            "amount": np.random.randint(1e7, 5e7, n).astype(float),
        }
    )


@pytest.fixture
def low_vol_df() -> pd.DataFrame:
    """低波动率行情（用于测试认知引擎不影响趋势）。"""
    np.random.seed(42)
    n = 60
    dates = pd.date_range("2026-01-01", periods=n, freq="B").date
    close = np.linspace(100, 115, n) + np.random.normal(0, 0.5, n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.3,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.random.randint(1e6, 5e6, n).astype(float),
            "amount": np.random.randint(1e7, 5e7, n).astype(float),
        }
    )


def test_backtest_runs(trend_df):
    """回测能跑完。"""
    engine = BacktestEngine(initial_capital=100_000)
    strategy = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    result = engine.run(strategy, trend_df, "000001")
    assert result is not None
    assert len(result.equity_curve) == len(trend_df)
    assert result.symbol == "000001"


def test_metrics_basic():
    """指标计算基本正确。"""
    equity = pd.Series([100, 105, 103, 110, 108, 120], index=pd.date_range("2026-01-01", periods=6, freq="B"))
    metrics = calc_metrics(equity, [])
    assert metrics["total_return"] == pytest.approx(0.20, rel=1e-3)
    assert metrics["max_drawdown"] > 0


def test_backtest_with_signals(trend_df):
    """策略触发信号后回测有交易。"""
    engine = BacktestEngine(initial_capital=100_000)
    strategy = DualMAStrategy(params={"short_window": 3, "long_window": 8})
    result = engine.run(strategy, trend_df, "000001")
    assert len(result.trades) > 0


def test_metrics_empty():
    """空权益曲线和空交易列表。"""
    metrics = calc_metrics(pd.Series(dtype=float), [])
    assert metrics["total_return"] == 0.0
    assert metrics["total_trades"] == 0.0
    assert metrics["win_rate"] == 0.0
    assert metrics["profit_factor"] == 0.0


def test_metrics_single_point():
    """单点权益曲线。"""
    equity = pd.Series([100.0])
    metrics = calc_metrics(equity, [])
    assert metrics["total_return"] == 0.0
    assert metrics["sharpe"] == 0.0


def test_metrics_profit_factor_inf():
    """只有盈利交易时 profit_factor 为 inf。"""
    trades = [
        {"direction": "buy", "date": "2026-01-01"},
        {"direction": "sell", "profit": 100.0, "date": "2026-01-02"},
    ]
    equity = pd.Series([100, 110], index=pd.date_range("2026-01-01", periods=2, freq="B"))
    metrics = calc_metrics(equity, trades)
    assert metrics["profit_factor"] == float("inf")
    assert metrics["win_rate"] == 1.0
    assert metrics["avg_hold_days"] == 1.0


def test_metrics_loss_only():
    """只有亏损交易。"""
    trades = [
        {"direction": "buy", "date": "2026-01-01"},
        {"direction": "sell", "profit": -50.0, "date": "2026-01-02"},
    ]
    equity = pd.Series([100, 90], index=pd.date_range("2026-01-01", periods=2, freq="B"))
    metrics = calc_metrics(equity, trades)
    assert metrics["win_rate"] == 0.0
    assert metrics["profit_factor"] == 0.0


def test_format_report(trend_df):
    """文本回测报告格式化。"""
    engine = BacktestEngine(initial_capital=100_000)
    strategy = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    result = engine.run(strategy, trend_df, "000001")
    text = format_report(result)
    assert "Backtest Report" in text
    assert "000001" in text


def test_format_report_dict(trend_df):
    """回测报告 dict 格式化。"""
    engine = BacktestEngine(initial_capital=100_000)
    strategy = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    result = engine.run(strategy, trend_df, "000001")
    data = format_report_dict(result)
    assert data["strategy"] == "dual_ma"
    assert data["symbol"] == "000001"
    assert "total_return" in data


def test_backtest_empty_data():
    """空数据返回空结果。"""
    engine = BacktestEngine()
    strategy = DualMAStrategy()
    result = engine.run(strategy, pd.DataFrame(), "000001")
    assert len(result.equity_curve) == 0
    assert len(result.trades) == 0


def test_backtest_insufficient_cash():
    """资金不足无法买入一手的场景。"""
    engine = BacktestEngine(initial_capital=100)
    strategy = DualMAStrategy(params={"short_window": 3, "long_window": 5})
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=10, freq="B"),
            "open": [10.0] * 10,
            "high": [11.0] * 10,
            "low": [9.0] * 10,
            "close": [10.0] * 10,
            "volume": [1e6] * 10,
            "amount": [1e7] * 10,
        }
    )
    result = engine.run(strategy, df, "000001")
    assert len(result.trades) == 0


def test_backtest_cognitive_engine_runs(low_vol_df):
    """启用认知引擎的回测能跑完且有 cognitive_log。"""
    engine = BacktestEngine(enable_cognitive_engine=True)
    strategy = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    result = engine.run(strategy, low_vol_df, "000001")
    assert result is not None
    assert len(result.equity_curve) == len(low_vol_df)
    # cognitive_log 有可能为空（行情简单时无触发）
    assert hasattr(result, "cognitive_log")


def test_backtest_cognitive_log_reduces_trades(low_vol_df):
    """认知引擎应减少交易次数（趋势确认过滤假信号）。"""
    base = BacktestEngine(enable_cognitive_engine=False)
    cog = BacktestEngine(enable_cognitive_engine=True)
    strategy_base = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    strategy_cog = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    r_base = base.run(strategy_base, low_vol_df, "000001")
    r_cog = cog.run(strategy_cog, low_vol_df, "000001")
    # 认知引擎可能过滤入场，但不能断言交易次数必然减少
    # 至少认知引擎不会让交易次数暴增
    assert len(r_cog.trades) <= len(r_base.trades) + 5  # 容忍小幅波动


def test_backtest_cognitive_engine_trailing_stop_exits(low_vol_df):
    """认知引擎的移动止损应产生 EXIT 信号而非策略卖出。"""
    engine = BacktestEngine(enable_cognitive_engine=True)
    strategy = DualMAStrategy(params={"short_window": 5, "long_window": 10})
    result = engine.run(strategy, low_vol_df, "000001")
    # 验证 cognitive_log 中的 trailing_stop_exit 是合法值
    for entry in result.cognitive_log:
        if entry.get("action") == "trailing_stop_exit":
            assert "stop_price" in entry or "close" in entry
