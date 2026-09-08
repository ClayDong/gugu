"""统一基准比较口径。

所有事件研究和连续净值比较都应使用共同交易日的复利收益，避免把
简单收益差、端点缺失和不同日期窗口混在一起。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CompoundBenchmarkResult:
    strategy_return: float
    benchmark_return: float
    excess_return: float
    information_ratio: float
    beta: float
    correlation: float
    common_days: int


def compound_return(values: pd.Series | np.ndarray) -> float:
    values = pd.Series(values, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    if values.empty:
        return 0.0
    return float((1.0 + values).prod() - 1.0)


def compare_returns(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
) -> CompoundBenchmarkResult:
    """按共同日期复利比较策略与基准。"""
    common = (
        pd.concat(
            [strategy_returns.rename("strategy"), benchmark_returns.rename("benchmark")], axis=1
        )
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )
    if len(common) < 2:
        return CompoundBenchmarkResult(0.0, 0.0, 0.0, 0.0, 1.0, 0.0, len(common))
    strategy = common["strategy"]
    benchmark = common["benchmark"]
    sr = compound_return(strategy)
    br = compound_return(benchmark)
    excess = (1 + sr) / (1 + br) - 1 if br > -1 else 0.0
    diff = strategy - benchmark
    te = float(diff.std(ddof=1) * np.sqrt(252)) if len(diff) > 1 else 0.0
    ir = float((excess / te) if te > 0 else 0.0)
    beta_den = float(benchmark.var(ddof=1))
    beta = float(strategy.cov(benchmark) / beta_den) if beta_den > 0 else 1.0
    corr = float(strategy.corr(benchmark)) if strategy.std() > 0 and benchmark.std() > 0 else 0.0
    return CompoundBenchmarkResult(sr, br, excess, ir, beta, corr, len(common))


def compare_price_series(
    strategy_equity: pd.Series,
    benchmark_close: pd.Series,
) -> CompoundBenchmarkResult:
    """从策略净值与基准收盘价生成共同日期复利比较。"""
    equity = strategy_equity.sort_index().pct_change().dropna()
    benchmark = benchmark_close.sort_index().pct_change().dropna()
    return compare_returns(equity, benchmark)
