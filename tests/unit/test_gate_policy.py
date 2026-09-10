"""统一策略准入门禁测试。"""

from __future__ import annotations

import math

from gugu.validation.gate_policy import GATE_VERSION, GatePolicy


def _passing_metrics(**overrides: float) -> dict:
    """返回一组在 gate-v2 全部门槛边界通过的指标。"""
    base = {
        "total_trades": 100,
        "avg_return": 0.0,
        "median_return": 0.0,
        "avg_sharpe": 0.001,
        "max_drawdown": 0.249,
        "profitable_ratio": 0.50,
        "profit_factor": 1.5,
        "param_stability": 0.80,
        "paper_signals": 30,
        "paper_return": 0.0,
    }
    base.update(overrides)
    return base


class TestGatePolicy:
    def test_full_gate_passes_at_inclusive_boundaries(self):
        report = GatePolicy.evaluate(_passing_metrics())
        assert report["passed"] is True
        assert report["version"] == GATE_VERSION

    def test_strict_sharpe_and_drawdown_boundaries_fail(self):
        report = GatePolicy.evaluate(_passing_metrics(avg_sharpe=0.0, max_drawdown=0.25))
        assert report["passed"] is False
        assert "avg_sharpe" in report["failed_checks"]
        assert "max_drawdown" in report["failed_checks"]

    def test_missing_or_non_finite_metrics_fail_closed(self):
        report = GatePolicy.evaluate(_passing_metrics(avg_sharpe=math.nan, paper_return=None))
        assert report["passed"] is False
        assert "avg_sharpe" in report["failed_checks"]
        assert "paper_return" in report["failed_checks"]

    def test_profit_factor_missing_fails_closed(self):
        metrics = _passing_metrics()
        del metrics["profit_factor"]
        report = GatePolicy.evaluate(metrics)
        assert report["passed"] is False
        assert "profit_factor" in report["failed_checks"]

    def test_profit_factor_below_threshold_fails(self):
        report = GatePolicy.evaluate(_passing_metrics(profit_factor=1.4))
        assert report["passed"] is False
        assert "profit_factor" in report["failed_checks"]

    def test_writeback_uses_same_stability_threshold(self):
        report = GatePolicy.evaluate_writeback(
            {
                "avg_sharpe": 0.1,
                "param_stability": 0.79,
                "max_drawdown": 0.1,
                "avg_return": 0.01,
                "profit_factor": 2.0,
                "oos_trades": 30,
            }
        )
        assert report["passed"] is False
        assert report["failed_checks"] == ["param_stability"]

    def test_writeback_profit_factor_missing_fails(self):
        report = GatePolicy.evaluate_writeback(
            {
                "avg_sharpe": 0.1,
                "param_stability": 0.9,
                "max_drawdown": 0.1,
                "avg_return": 0.01,
                "oos_trades": 30,
            }
        )
        assert report["passed"] is False
        assert "profit_factor" in report["failed_checks"]


class TestCategoryDrawdown:
    """gate-v2: 按策略类型分层最大回撤门槛。"""

    def test_short_term_category_uses_15pct_drawdown(self):
        """短线策略(trend/breakout/mean_revert)回撤门槛 15%。"""
        report = GatePolicy.evaluate(
            _passing_metrics(max_drawdown=0.20),
            category="trend",
        )
        assert report["passed"] is False
        assert "max_drawdown" in report["failed_checks"]
        assert report["checks"]["max_drawdown"]["threshold"] == 0.15

    def test_short_term_category_passes_at_14pct(self):
        report = GatePolicy.evaluate(
            _passing_metrics(max_drawdown=0.14),
            category="breakout",
        )
        assert report["passed"] is True

    def test_long_term_category_uses_25pct_drawdown(self):
        """中长线策略(main_force)回撤门槛 25%（保持不变）。"""
        report = GatePolicy.evaluate(
            _passing_metrics(max_drawdown=0.20),
            category="main_force",
        )
        assert report["passed"] is True
        assert report["checks"]["max_drawdown"]["threshold"] == 0.25

    def test_no_category_uses_default_25pct(self):
        """不传 category 时用默认 25%。"""
        report = GatePolicy.evaluate(_passing_metrics(max_drawdown=0.20))
        assert report["passed"] is True
        assert report["checks"]["max_drawdown"]["threshold"] == 0.25


def test_classify_evidence_marks_low_sample_without_lowering_gate():
    verdict = GatePolicy.evaluate_writeback(
        {
            "avg_sharpe": 1.0,
            "param_stability": 1.0,
            "max_drawdown": 0.1,
            "avg_return": 0.1,
            "profit_factor": 2.0,
            "oos_trades": 8,
        }
    )
    assert verdict["passed"] is False
    assert GatePolicy.classify_evidence(verdict) == "insufficient_evidence"


def test_classify_evidence_marks_full_sample_failure():
    verdict = GatePolicy.evaluate_writeback(
        {
            "avg_sharpe": -0.1,
            "param_stability": 1.0,
            "max_drawdown": 0.1,
            "avg_return": -0.1,
            "profit_factor": 2.0,
            "oos_trades": 30,
        }
    )
    assert GatePolicy.classify_evidence(verdict) == "failed"
