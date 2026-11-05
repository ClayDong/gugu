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
        "selection_alpha": 0.0001,
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

    def test_negative_max_drawdown_rejected_false_pass(self):
        """回归：负值回撤（如 -0.24）不得被 '数值 < 阈值' 规则假通过。"""
        # mean_revert 分层门槛 15%；-0.24 < 0.15 本会被误判通过，加固后应判失败。
        report = GatePolicy.evaluate(
            _passing_metrics(max_drawdown=-0.24),
            category="mean_revert",
        )
        assert report["passed"] is False
        assert "max_drawdown" in report["failed_checks"]

    def test_positive_max_drawdown_still_evaluated_normally(self):
        """正值回撤仍按阈值正常评估，不受负值防御影响。"""
        report = GatePolicy.evaluate(
            _passing_metrics(max_drawdown=0.10),
            category="mean_revert",
        )
        assert report["checks"]["max_drawdown"]["passed"] is True


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


def test_classify_evidence_falls_back_to_total_trades():
    """回归：manifest 派生裁决缺 oos_trades 时回退到 total_trades，不至于一律 insufficient。"""
    metrics = {
        "total_trades": 150,
        "avg_return": 0.02,
        "median_return": 0.01,
        "avg_sharpe": 0.15,
        "max_drawdown": 0.10,
        "profitable_ratio": 0.55,
        "profit_factor": 2.0,
        "param_stability": 0.9,
        "paper_signals": 40,
        "paper_return": 0.01,
        "selection_alpha": 0.0005,
    }
    verdict = GatePolicy.evaluate(metrics, category="mean_revert")
    assert verdict["passed"] is True
    # 默认 sample_key=oos_trades 在 manifest 中缺失 → 回退 total_trades=150 ≥ 30
    assert GatePolicy.classify_evidence(verdict) == "passed"
    # 显式请求 oos_trades 且缺失时，仍按原语义判 insufficient_evidence
    assert GatePolicy.classify_evidence(verdict, sample_key="oos_trades") == "insufficient_evidence"


class TestSelectionAlpha:
    """选股 alpha 门禁（2026-11-05 新增）。

    为什么单列一组：这条规则不是"再多一个指标"，它纠正的是一个会反复出现
    的误判——把池子同期涨幅当成策略能力。实测三个在用策略净alpha 为正或近零，
    拆开后选股alpha 全部为负。所以它的边界必须被钉死，尤其是"恰好 0"。
    """

    def test_missing_selection_alpha_fails(self):
        metrics = _passing_metrics()
        metrics.pop("selection_alpha")
        report = GatePolicy.evaluate(metrics)

        assert report["passed"] is False
        assert "selection_alpha" in report["failed_checks"]

    def test_zero_selection_alpha_fails(self):
        """恰好 0 意味着与池子其他股票无差别：那是市场状态指示器，不是选股策略。"""
        report = GatePolicy.evaluate(_passing_metrics(selection_alpha=0.0))

        assert report["passed"] is False
        assert "selection_alpha" in report["failed_checks"]

    def test_negative_selection_alpha_fails(self):
        """实测值：box_breakout -0.20%、linjudaye -0.06%、double_yin -0.15%。"""
        for value in (-0.0020, -0.0006, -0.0015):
            report = GatePolicy.evaluate(_passing_metrics(selection_alpha=value))
            assert report["passed"] is False
            assert "selection_alpha" in report["failed_checks"]

    def test_positive_selection_alpha_passes(self):
        report = GatePolicy.evaluate(_passing_metrics(selection_alpha=0.0001))

        assert report["passed"] is True
        assert report["checks"]["selection_alpha"]["passed"] is True

    def test_non_finite_selection_alpha_fails(self):
        for value in (math.nan, math.inf, "N/A", None):
            report = GatePolicy.evaluate(_passing_metrics(selection_alpha=value))
            assert report["passed"] is False
            assert "selection_alpha" in report["failed_checks"]

    def test_selection_alpha_is_in_thresholds(self):
        """阈值映射必须包含它，否则报告层会漏印这条检查。"""
        assert GatePolicy.thresholds()["selection_alpha"] == 0.0
        assert "selection_alpha" in GatePolicy.rules()
