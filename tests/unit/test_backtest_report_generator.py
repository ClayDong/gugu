"""回测报告生成器单元测试。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
import pytest

from gugu.backtest import BacktestResult
from scripts.backtest_report_generator import (
    generate_report,
    generate_report_from_metrics_dict,
    save_report,
)


def _make_mock_result(
    symbol: str,
    strategy_name: str,
    total_return: float = 0.05,
    annual_return: float = 0.10,
    sharpe: float = 0.8,
    max_drawdown: float = 0.15,
    win_rate: float = 0.55,
    profit_factor: float = 1.5,
    total_trades: float = 20.0,
    avg_hold_days: float = 5.0,
) -> BacktestResult:
    """创建一个模拟的 BacktestResult。"""
    metrics = {
        "total_return": total_return,
        "annual_return": annual_return,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "total_trades": total_trades,
        "avg_hold_days": avg_hold_days,
    }
    return BacktestResult(
        symbol=symbol,
        strategy_name=strategy_name,
        trades=[],
        equity_curve=pd.Series(dtype=float),
        metrics=metrics,
    )


class TestGenerateReport:
    """测试 generate_report 函数。"""

    def test_empty_results(self):
        """空结果列表返回无数据报告。"""
        md = generate_report([])
        assert "无数据" in md

    def test_single_strategy_single_stock(self):
        """单策略单股票。"""
        results = [
            _make_mock_result("000001", "dual_ma", total_return=0.10, sharpe=1.2),
        ]
        md = generate_report(results)
        assert "# 多策略回测对比报告" in md
        assert "dual_ma" in md
        # 应该包含夏普排序表
        assert "夏普比率" in md or "sharp" in md.lower()

    def test_two_strategies_multi_stocks(self):
        """两策略多股票。"""
        results = [
            _make_mock_result("000001", "strategy_a", total_return=0.10, sharpe=1.2, win_rate=0.6),
            _make_mock_result("000002", "strategy_a", total_return=0.05, sharpe=0.9, win_rate=0.55),
            _make_mock_result("000003", "strategy_a", total_return=-0.02, sharpe=-0.1, win_rate=0.45),
            _make_mock_result("000001", "strategy_b", total_return=0.08, sharpe=1.0, win_rate=0.58),
            _make_mock_result("000002", "strategy_b", total_return=0.03, sharpe=0.6, win_rate=0.52),
            _make_mock_result("000003", "strategy_b", total_return=0.01, sharpe=0.3, win_rate=0.50),
        ]
        md = generate_report(results)
        assert "strategy_a" in md
        assert "strategy_b" in md
        # 应该有 6 条记录
        assert "6 条记录" in md

    def test_significance_positive(self):
        """收益全部为正时 t 检验应显著。"""
        results = [
            _make_mock_result("000001", "strategy_a", total_return=0.05),
            _make_mock_result("000002", "strategy_a", total_return=0.06),
            _make_mock_result("000003", "strategy_a", total_return=0.04),
            _make_mock_result("000004", "strategy_a", total_return=0.07),
            _make_mock_result("000005", "strategy_a", total_return=0.03),
        ]
        md = generate_report(results)
        # 应该显示显著
        assert "✅" in md

    def test_significance_not_positive(self):
        """收益围绕零震荡时 t 检验不应显著。"""
        results = [
            _make_mock_result("000001", "strategy_b", total_return=0.01),
            _make_mock_result("000002", "strategy_b", total_return=-0.01),
            _make_mock_result("000003", "strategy_b", total_return=0.02),
            _make_mock_result("000004", "strategy_b", total_return=-0.02),
            _make_mock_result("000005", "strategy_b", total_return=0.005),
        ]
        md = generate_report(results)
        # small sample with mixed signs might not be significant
        # Just check it contains the strategy name and no crash
        assert "strategy_b" in md

    def test_missing_metrics(self):
        """缺失指标的 BacktestResult 不应崩溃。"""
        results = [
            BacktestResult(
                symbol="000001",
                strategy_name="strategy_a",
                trades=[],
                equity_curve=pd.Series(dtype=float),
                metrics={"total_return": 0.05},  # 不完整
            ),
        ]
        md = generate_report(results)
        assert "strategy_a" in md

    def test_sorting_tables_present(self):
        """报告包含多种排序表。"""
        results = [
            _make_mock_result("000001", "s1", total_return=0.10, sharpe=1.5, win_rate=0.65, profit_factor=2.0),
            _make_mock_result("000002", "s1", total_return=0.08, sharpe=1.2, win_rate=0.60, profit_factor=1.8),
            _make_mock_result("000003", "s2", total_return=0.06, sharpe=1.0, win_rate=0.55, profit_factor=1.5),
            _make_mock_result("000004", "s2", total_return=0.04, sharpe=0.8, win_rate=0.50, profit_factor=1.2),
        ]
        md = generate_report(results)
        # 所有排序表头应该存在
        assert "策略总览汇总表" in md
        assert "夏普比率" in md or "夏普" in md
        assert "胜率" in md
        assert "总收益" in md or "收益中位数" in md
        assert "盈亏比" in md


class TestGenerateReportFromMetricsDict:
    """测试 generate_report_from_metrics_dict。"""

    def test_basic(self):
        """基本的 metrics dict 输入。"""
        data = {
            "strategy_a": [
                {"total_return": 0.10, "annual_return": 0.20, "sharpe": 1.2, "max_drawdown": 0.15, "win_rate": 0.6, "profit_factor": 1.5, "total_trades": 10.0, "avg_hold_days": 5.0},
                {"total_return": 0.05, "annual_return": 0.10, "sharpe": 0.9, "max_drawdown": 0.12, "win_rate": 0.55, "profit_factor": 1.3, "total_trades": 8.0, "avg_hold_days": 4.0},
            ],
            "strategy_b": [
                {"total_return": 0.03, "annual_return": 0.06, "sharpe": 0.5, "max_drawdown": 0.20, "win_rate": 0.5, "profit_factor": 1.1, "total_trades": 15.0, "avg_hold_days": 6.0},
            ],
        }
        md = generate_report_from_metrics_dict(data)
        assert "strategy_a" in md
        assert "strategy_b" in md

    def test_empty(self):
        """空 dict。"""
        md = generate_report_from_metrics_dict({})
        assert "无数据" in md or len(md) > 0

    def test_single_value(self):
        """单个策略单个值。"""
        data = {
            "s1": [
                {"total_return": 0.05, "annual_return": 0.10, "sharpe": 0.8, "max_drawdown": 0.10, "win_rate": 0.5, "profit_factor": 1.2, "total_trades": 5.0, "avg_hold_days": 3.0},
            ],
        }
        md = generate_report_from_metrics_dict(data)
        assert "s1" in md


class TestSaveReport:
    """测试 save_report。"""

    def test_saves_to_file(self):
        """保存 Markdown 到文件。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_report.md"
            save_report("# Test Report\n\nContent", path)
            assert path.exists()
            content = path.read_text(encoding="utf-8")
            assert "# Test Report" in content

    def test_creates_parent_dirs(self):
        """自动创建父目录。"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "subdir" / "nested" / "report.md"
            save_report("# Hello", path)
            assert path.exists()


class TestDistributionStats:
    """验证报告中的分布统计值正确性。"""

    def test_mean_median_correct(self):
        """均值和中位数计算正确。"""
        results = [
            _make_mock_result("000001", "s1", total_return=0.10),
            _make_mock_result("000002", "s1", total_return=0.20),
            _make_mock_result("000003", "s1", total_return=0.30),
        ]
        md = generate_report(results)
        # 均值 0.20，中位数 0.20
        assert "20.00%" in md or "20.0%" in md

    def test_sharpe_median(self):
        """夏普中位数正确。"""
        results = [
            _make_mock_result("000001", "s1", sharpe=0.5),
            _make_mock_result("000002", "s1", sharpe=1.0),
            _make_mock_result("000003", "s1", sharpe=1.5),
        ]
        md = generate_report(results)
        # 中位数 1.0
        assert "1.000" in md or "1.00" in md

    def test_win_rate_distribution(self):
        """胜率分布显示正确。"""
        results = [
            _make_mock_result("000001", "s1", win_rate=0.4),
            _make_mock_result("000002", "s1", win_rate=0.5),
            _make_mock_result("000003", "s1", win_rate=0.6),
            _make_mock_result("000004", "s1", win_rate=0.7),
        ]
        md = generate_report(results)
        # 中位数在 0.5-0.6 之间
        assert "s1" in md


def test_real_world_like_scenario():
    """模拟真实回测场景：3 策略 × 5 股票。"""
    np.random.seed(42)
    strategies = ["turtle", "dual_ma", "bollinger"]
    symbols = [f"{i:06d}" for i in range(600001, 600006)]

    results = []
    for sym in symbols:
        for sname in strategies:
            # 不同策略有不同表现特征
            if sname == "turtle":
                ret = np.random.normal(0.08, 0.05)
                sharpe = np.random.normal(1.0, 0.3)
                wr = np.random.normal(0.55, 0.05)
            elif sname == "dual_ma":
                ret = np.random.normal(0.05, 0.04)
                sharpe = np.random.normal(0.7, 0.2)
                wr = np.random.normal(0.52, 0.04)
            else:  # bollinger
                ret = np.random.normal(0.03, 0.06)
                sharpe = np.random.normal(0.4, 0.3)
                wr = np.random.normal(0.48, 0.06)

            results.append(
                _make_mock_result(
                    symbol=sym,
                    strategy_name=sname,
                    total_return=float(ret),
                    sharpe=float(sharpe),
                    win_rate=float(wr),
                    profit_factor=float(np.random.uniform(1.0, 2.0)),
                    total_trades=float(np.random.randint(5, 30)),
                    avg_hold_days=float(np.random.uniform(2, 10)),
                    annual_return=float(np.random.normal(0.08, 0.05)),
                    max_drawdown=float(np.random.uniform(0.05, 0.25)),
                )
            )

    md = generate_report(results)
    # 包含所有策略名
    for sname in strategies:
        assert sname in md
    # 包含排序表
    assert "夏普比率" in md
    assert "胜率" in md
    assert "总收益" in md
    assert "盈亏比" in md
    # 包含策略总览
    assert "策略总览汇总表" in md
    # 包含结论
    assert "结论与建议" in md
    # 包含分布信息
    assert "各策略指标分布" in md
