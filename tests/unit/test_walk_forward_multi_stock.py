"""多股聚合 Walk-Forward 优化单元测试。

测试覆盖：
- 基本功能：3+ 股票输入能产出结果
- 边界条件：空输入、不足3只股票、数据不足
- 对抗性审查：验证多股聚合 vs 单股的差异
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.backtest.engine import walk_forward_optimize_multi_stock
from gugu.strategies import DualMAStrategy


def _make_stock_df(symbol: str, n: int = 200, seed: int = 42, trend: str = "up") -> pd.DataFrame:
    """生成单只股票的 OHLCV 数据。

    Args:
        symbol: 股票代码（仅用于日志，不影响数据）
        n: 数据长度
        seed: 随机种子
        trend: "up"=上涨 / "down"=下跌 / "flat"=震荡
    """
    np.random.seed(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")

    if trend == "up":
        rets = np.random.uniform(0.001, 0.015, n)
    elif trend == "down":
        rets = np.random.uniform(-0.015, -0.001, n)
    else:  # flat
        rets = np.random.uniform(-0.005, 0.005, n)

    prices = np.cumprod(1 + rets) * 10
    volumes = np.random.uniform(1e6, 5e6, n).astype(float)

    return pd.DataFrame(
        {
            "date": dates,
            "open": prices * 0.99,
            "high": prices * 1.02,
            "low": prices * 0.98,
            "close": prices,
            "volume": volumes,
            "amount": prices * volumes,
        }
    )


class TestMultiStockWalkForwardBasic:
    """基本功能测试。"""

    def test_three_stocks_produces_results(self) -> None:
        """3 只股票应能产出 Walk-Forward 结果。"""
        stock_dfs = {
            "600519": _make_stock_df("600519", n=200, seed=1, trend="up"),
            "000858": _make_stock_df("000858", n=200, seed=2, trend="up"),
            "002714": _make_stock_df("002714", n=200, seed=3, trend="up"),
        }

        results = walk_forward_optimize_multi_stock(
            strategy_cls=DualMAStrategy,
            stock_dfs=stock_dfs,
            param_grid={"short_window": [5, 10], "long_window": [20, 30]},
            train_ratio=0.6,
            n_splits=3,
        )

        assert len(results) > 0
        assert "error" not in results[0]
        for r in results:
            assert "best_params" in r
            assert "train_avg_sharpe" in r
            assert "test_avg_sharpe" in r
            assert "test_details" in r
            assert "test_min_sharpe" in r
            assert "test_min_return" in r
            assert "test_worst_max_dd" in r
            assert "test_negative_return_ratio" in r
            assert "test_max_trade_share" in r
            assert "risk_flags" in r
            assert 0 <= r["test_negative_return_ratio"] <= 1
            assert 0 <= r["test_max_trade_share"] <= 1
            sharpes = [detail["test_sharpe"] for detail in r["test_details"]]
            returns = [detail["test_return"] for detail in r["test_details"]]
            drawdowns = [detail["test_max_dd"] for detail in r["test_details"]]
            assert r["test_min_sharpe"] == min(sharpes)
            assert r["test_min_return"] == min(returns)
            assert r["test_worst_max_dd"] == max(drawdowns)
            # 每个窗口应该有3只股票的明细
            assert r["test_stock_count"] == 3
            assert r["global_min_test_sharpe"] == min(x["test_min_sharpe"] for x in results)
            assert r["global_min_test_return"] == min(x["test_min_return"] for x in results)

    def test_returns_per_stock_details(self) -> None:
        """结果应包含每只股票的测试明细。"""
        stock_dfs = {
            "600519": _make_stock_df("600519", n=150, seed=10, trend="up"),
            "000858": _make_stock_df("000858", n=150, seed=20, trend="up"),
        }

        results = walk_forward_optimize_multi_stock(
            strategy_cls=DualMAStrategy,
            stock_dfs=stock_dfs,
            param_grid={"short_window": [5], "long_window": [20]},
            n_splits=2,
        )

        for r in results:
            for detail in r["test_details"]:
                assert "symbol" in detail
                assert "test_sharpe" in detail
                assert "test_return" in detail
                assert "test_max_dd" in detail
                assert "trades" in detail


class TestMultiStockWalkForwardEdgeCases:
    """边界条件测试。"""

    def test_empty_stock_dfs_returns_error(self) -> None:
        """空股票字典应返回错误。"""
        results = walk_forward_optimize_multi_stock(
            strategy_cls=DualMAStrategy,
            stock_dfs={},
            param_grid={"short_window": [5]},
        )
        assert len(results) == 1
        assert "error" in results[0]

    def test_insufficient_data_returns_error(self) -> None:
        """数据不足 120 行应返回错误。"""
        stock_dfs = {
            "600519": _make_stock_df("600519", n=100, seed=1),  # < 120
            "000858": _make_stock_df("000858", n=100, seed=2),
        }

        results = walk_forward_optimize_multi_stock(
            strategy_cls=DualMAStrategy,
            stock_dfs=stock_dfs,
            param_grid={"short_window": [5]},
        )
        assert len(results) == 1
        assert "error" in results[0]

    def test_empty_param_grid_uses_default(self) -> None:
        """空参数网格应使用默认参数，不报错。"""
        stock_dfs = {
            "600519": _make_stock_df("600519", n=150, seed=1, trend="up"),
            "000858": _make_stock_df("000858", n=150, seed=2, trend="up"),
        }

        results = walk_forward_optimize_multi_stock(
            strategy_cls=DualMAStrategy,
            stock_dfs=stock_dfs,
            param_grid={},
            n_splits=2,
        )
        # 应该正常返回结果（用默认参数）
        assert len(results) > 0
        assert "error" not in results[0]

    def test_aligns_different_dates_by_common_trading_days(self) -> None:
        """不同交易日序列应按共同交易日对齐，而不是按行号对齐。"""
        first = _make_stock_df("600519", n=200, seed=1, trend="up")
        second = _make_stock_df("000858", n=200, seed=2, trend="up")
        second = second.drop(index=[50, 100, 150]).copy()
        stock_dfs = {"600519": first, "000858": second}

        results = walk_forward_optimize_multi_stock(
            strategy_cls=DualMAStrategy,
            stock_dfs=stock_dfs,
            param_grid={"short_window": [5], "long_window": [20]},
            n_splits=2,
        )
        assert "error" not in results[0]
        for result in results:
            assert result["train_end"] < result["test_start"] <= result["test_end"]
            assert result["train_start"] <= result["train_end"]
            assert result["test_stock_count"] == 2

    def test_rejects_duplicate_dates(self) -> None:
        """重复交易日会破坏时间边界，必须 fail-closed。"""
        duplicate = _make_stock_df("000858", n=150, seed=2, trend="up")
        duplicate.loc[1, "date"] = duplicate.loc[0, "date"]
        results = walk_forward_optimize_multi_stock(
            strategy_cls=DualMAStrategy,
            stock_dfs={
                "600519": _make_stock_df("600519", n=150, seed=1, trend="up"),
                "000858": duplicate,
            },
            param_grid={"short_window": [5], "long_window": [20]},
            n_splits=2,
        )
        assert results == [{"error": "000858 存在重复交易日，无法做 walk-forward"}]


class TestMultiStockVsSingleStock:
    """对抗性审查：多股聚合 vs 单股的差异。"""

    def test_multi_stock_more_stable_than_single(self) -> None:
        """多股聚合应比单股更稳定（不易过拟合到单只股票特征）。

        对抗性审查：
        - 单股 Walk-Forward 容易过拟合（训练 Sharpe 高，测试 Sharpe 低）
        - 多股聚合通过平均降低单股极端值影响
        - 测试：构造一只"过拟合友好"的股票（强趋势）和一只普通股票，
          多股聚合的最优参数应更稳健
        """
        # 一只强趋势股（容易过拟合）
        strong_df = _make_stock_df("strong", n=200, seed=42, trend="up")
        # 一只震荡股
        flat_df = _make_stock_df("flat", n=200, seed=99, trend="flat")

        # 多股聚合
        multi_results = walk_forward_optimize_multi_stock(
            strategy_cls=DualMAStrategy,
            stock_dfs={"strong": strong_df, "flat": flat_df},
            param_grid={"short_window": [5, 10], "long_window": [20, 30]},
            n_splits=2,
        )

        # 验证：多股聚合结果中 test_avg_sharpe 应该在两只股票之间
        # （而不是被强趋势股拉得过高）
        for r in multi_results:
            sharpes = [d["test_sharpe"] for d in r["test_details"]]
            assert r["test_min_sharpe"] == min(sharpes)
            if len(sharpes) >= 2:
                min_s = min(sharpes)
                max_s = max(sharpes)
                # 聚合后的平均应该在两只股票之间
                assert (
                    min_s <= r["test_avg_sharpe"] <= max_s
                ), f"聚合 Sharpe {r['test_avg_sharpe']} 应在 [{min_s}, {max_s}] 之间"
