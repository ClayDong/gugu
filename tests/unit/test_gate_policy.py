"""统一策略准入门禁测试。"""

from __future__ import annotations

import math

from gugu.validation.gate_policy import GatePolicy


class TestGatePolicy:
    def test_full_gate_passes_at_inclusive_boundaries(self):
        metrics = {
            "total_trades": 100,
            "avg_return": 0.0,
            "median_return": 0.0,
            "avg_sharpe": 0.001,
            "max_drawdown": 0.249,
            "profitable_ratio": 0.50,
            "param_stability": 0.80,
            "paper_signals": 30,
            "paper_return": 0.0,
        }
        report = GatePolicy.evaluate(metrics)
        assert report["passed"] is True
        assert report["version"] == "gate-v1"

    def test_strict_sharpe_and_drawdown_boundaries_fail(self):
        metrics = {
            "total_trades": 100,
            "avg_return": 0.0,
            "median_return": 0.0,
            "avg_sharpe": 0.0,
            "max_drawdown": 0.25,
            "profitable_ratio": 0.50,
            "param_stability": 0.80,
            "paper_signals": 30,
            "paper_return": 0.0,
        }
        report = GatePolicy.evaluate(metrics)
        assert report["passed"] is False
        assert "avg_sharpe" in report["failed_checks"]
        assert "max_drawdown" in report["failed_checks"]

    def test_missing_or_non_finite_metrics_fail_closed(self):
        metrics = {
            "total_trades": 100,
            "avg_return": 0.0,
            "median_return": 0.0,
            "avg_sharpe": math.nan,
            "max_drawdown": 0.1,
            "profitable_ratio": 0.5,
            "param_stability": 0.8,
            "paper_signals": 30,
        }
        report = GatePolicy.evaluate(metrics)
        assert report["passed"] is False
        assert "avg_sharpe" in report["failed_checks"]
        assert "paper_return" in report["failed_checks"]

    def test_writeback_uses_same_stability_threshold(self):
        report = GatePolicy.evaluate_writeback(
            {
                "avg_sharpe": 0.1,
                "param_stability": 0.79,
                "max_drawdown": 0.1,
                "avg_return": 0.01,
                "oos_trades": 30,
            }
        )
        assert report["passed"] is False
        assert report["failed_checks"] == ["param_stability"]
