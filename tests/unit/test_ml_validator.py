"""ML 模型验证与解释模块测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.analysis.ml_validator import (
    MLValidationReport,
    build_validation_report,
    compute_permutation_importance,
    detect_model_drift,
    explain_prediction,
    validate_model_predictions,
    validate_ml_strategy_oos,
)


class TestValidateModelPredictions:
    """测试模型预测验证。"""

    def test_perfect_predictions(self):
        """完美预测时指标应为 1。"""
        y_true = np.array([0, 0, 1, 1])
        y_pred = np.array([0, 0, 1, 1])
        y_score = np.array([0.1, 0.2, 0.8, 0.9])
        metrics = validate_model_predictions(y_true, y_pred, y_score)

        assert metrics["accuracy"] == 1.0
        assert metrics["precision"] == 1.0
        assert metrics["recall"] == 1.0
        assert metrics["f1"] == 1.0
        assert metrics["auc_roc"] > 0.99

    def test_random_predictions(self):
        """随机预测时准确率接近 0.5。"""
        np.random.seed(42)
        y_true = np.array([0, 1] * 50)
        y_pred = np.array([1, 0] * 50)
        metrics = validate_model_predictions(y_true, y_pred)

        assert metrics["accuracy"] == pytest.approx(0.0, abs=0.01)
        assert metrics["precision"] == 0.0

    def test_ic_computation(self):
        """IC 计算应能捕捉预测分数与未来收益的相关性。"""
        np.random.seed(42)
        n = 100
        score = np.random.randn(n)
        forward_ret = 0.5 * score + 0.3 * np.random.randn(n)
        metrics = validate_model_predictions(
            y_true=np.zeros(n),
            y_pred=np.zeros(n),
            y_score=score,
            forward_returns=forward_ret,
        )

        assert metrics["ic"] > 0.3


class TestPermutationImportance:
    """测试排列重要性。"""

    def test_permutation_importance_identifies_important_feature(self):
        """排列重要性应识别出重要特征。"""
        np.random.seed(42)
        n = 200
        important = np.random.randn(n)
        noise = np.random.randn(n)
        y = (important > 0).astype(int)
        X = pd.DataFrame({"important": important, "noise": noise})

        def score_func(df, labels):
            pred = (df["important"] > 0).astype(int)
            return float(np.mean(pred == labels))

        importance = compute_permutation_importance(X, y, score_func, n_repeats=3)

        assert importance["important"] > importance["noise"]
        assert all(v >= 0 for v in importance.values())


class TestModelDrift:
    """测试模型漂移检测。"""

    def test_no_drift_for_identical_distribution(self):
        """相同分布时不应判定漂移。"""
        np.random.seed(42)
        ref = np.random.randn(1000)
        cur = np.random.randn(1000)
        result = detect_model_drift(ref, cur, threshold=0.1)

        assert not result["is_drifted"]
        assert result["drift_score"] < 0.1

    def test_drift_for_shifted_distribution(self):
        """偏移分布应判定漂移。"""
        np.random.seed(42)
        ref = np.random.randn(1000)
        cur = np.random.randn(1000) + 2.0
        result = detect_model_drift(ref, cur, threshold=0.1)

        assert result["is_drifted"]
        assert result["drift_score"] > 0.1


class TestBuildValidationReport:
    """测试验证报告构建。"""

    def test_report_contains_recommendations(self):
        """报告应包含建议。"""
        y_true = np.array([0, 0, 1, 1])
        y_pred = np.array([0, 0, 1, 1])
        report = build_validation_report(y_true, y_pred)

        assert isinstance(report, MLValidationReport)
        assert len(report.recommendations) > 0
        assert report.accuracy == 1.0

    def test_low_accuracy_report_warns(self):
        """低准确率报告应给出警告。"""
        y_true = np.array([0, 0, 1, 1])
        y_pred = np.array([1, 1, 0, 0])
        report = build_validation_report(y_true, y_pred)

        assert any("准确率" in r for r in report.recommendations)


class TestExplainPrediction:
    """测试预测解释。"""

    def test_explain_deviation(self):
        """解释应返回特征偏离度。"""
        values = {"f1": 10.0, "f2": -5.0}
        baseline = {"f1": 5.0, "f2": -5.0}
        explanation = explain_prediction(values, baseline)

        assert explanation["f1"] == pytest.approx(1.0, abs=0.01)
        assert explanation["f2"] == pytest.approx(0.0, abs=0.01)


class TestValidateMLStrategyOOS:
    """测试 ML 策略 OOS 验证。"""

    def test_oos_validation_with_strong_signal(self):
        """强信号时应通过验证。"""
        np.random.seed(42)
        n = 100
        close = pd.Series(np.cumsum(np.random.randn(n) * 0.01) + 10)
        score = np.linspace(0.1, 0.9, n)
        signal = (score > 0.5).astype(int)
        predictions = pd.DataFrame({"score": score, "signal": signal})

        report = validate_ml_strategy_oos(predictions, close, forward_days=5)

        assert report.sample_count > 0
        assert 0.0 <= report.auc_roc <= 1.0

    def test_oos_validation_insufficient_samples(self):
        """样本不足时返回空报告。"""
        close = pd.Series(np.random.randn(10))
        predictions = pd.DataFrame({"score": np.random.rand(10), "signal": np.zeros(10)})

        report = validate_ml_strategy_oos(predictions, close, forward_days=5)

        assert report.sample_count < 30
        assert any("样本量不足" in r for r in report.recommendations)
