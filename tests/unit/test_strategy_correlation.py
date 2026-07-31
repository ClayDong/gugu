"""策略相关性分析模块测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.analysis.strategy_correlation import (
    StrategyCorrelationResult,
    analyze_correlation,
    build_signal_matrix,
    recommend_weights,
)


class TestBuildSignalMatrix:
    """测试信号矩阵构建。"""

    def test_build_signal_matrix_basic(self):
        """基本信号矩阵构建。"""
        signals = {
            "strategy_a": [("000001", 1, 0.8), ("000002", -1, 0.6)],
            "strategy_b": [("000001", 1, 0.7)],
        }
        symbols = ["000001", "000002"]
        matrix = build_signal_matrix(signals, symbols)

        assert list(matrix.columns) == ["strategy_a", "strategy_b"]
        assert "000001_buy" in matrix.index
        assert "000002_sell" in matrix.index
        assert matrix.loc["000001_buy", "strategy_a"] == pytest.approx(0.8)
        assert matrix.loc["000001_buy", "strategy_b"] == pytest.approx(0.7)
        assert matrix.loc["000002_sell", "strategy_a"] == pytest.approx(0.6)

    def test_build_signal_matrix_empty(self):
        """空信号构建。"""
        matrix = build_signal_matrix({}, ["000001"])
        assert matrix.empty


class TestAnalyzeCorrelation:
    """测试相关性分析。"""

    def test_perfect_redundancy_detected(self):
        """完全相关策略应被识别为冗余。"""
        # 两个策略信号完全一致
        matrix = pd.DataFrame(
            {
                "s1": [1.0, 0.0, 1.0, 0.0],
                "s2": [1.0, 0.0, 1.0, 0.0],
            }
        )
        result = analyze_correlation(matrix, redundancy_threshold=0.7)

        assert len(result.redundant_pairs) == 1
        assert result.redundant_pairs[0][2] == pytest.approx(1.0)

    def test_complementary_detected(self):
        """负相关策略应被识别为互补。"""
        # 构造负相关数据，确保落在 (-0.7, -0.3] 区间
        np.random.seed(42)
        n = 300
        x = np.random.randn(n)
        y = -0.55 * x + 0.7 * np.random.randn(n)
        matrix = pd.DataFrame({"s1": x, "s2": y})
        result = analyze_correlation(
            matrix,
            redundancy_threshold=0.7,
            complementary_threshold=-0.3,
        )

        assert len(result.complementary_pairs) >= 1
        assert result.complementary_pairs[0][2] < -0.3

    def test_independence_scores(self):
        """独立性评分应在 0-1 之间。"""
        matrix = pd.DataFrame(
            {
                "s1": [1.0, 0.0, 1.0, 0.0],
                "s2": [0.9, 0.1, 0.8, 0.2],
            }
        )
        result = analyze_correlation(matrix)

        for score in result.independence_scores.values():
            assert 0.0 <= score <= 1.0

    def test_empty_matrix(self):
        """空矩阵应返回空结果。"""
        result = analyze_correlation(pd.DataFrame())
        assert result.correlation_matrix.empty
        assert "策略数量不足" in result.recommendations[0]

    def test_constant_column(self):
        """常数列应被过滤。"""
        matrix = pd.DataFrame(
            {
                "s1": [1.0, 1.0, 1.0, 1.0],
                "s2": [1.0, 0.0, 1.0, 0.0],
            }
        )
        result = analyze_correlation(matrix)
        assert result.correlation_matrix.empty


class TestRecommendWeights:
    """测试权重推荐。"""

    def test_recommend_weights_normalizes(self):
        """推荐权重应归一化。"""
        result = StrategyCorrelationResult(
            correlation_matrix=pd.DataFrame(),
            redundant_pairs=[],
            complementary_pairs=[],
            independence_scores={"s1": 0.8, "s2": 0.6},
            recommendations=[],
        )
        base_weights = {"s1": 0.5, "s2": 0.5}
        recommended = recommend_weights(result, base_weights)

        assert sum(recommended.values()) == pytest.approx(1.0)
        assert "s1" in recommended
        assert "s2" in recommended

    def test_redundant_pair_downweights(self):
        """冗余策略对应被降权。"""
        result = StrategyCorrelationResult(
            correlation_matrix=pd.DataFrame(),
            redundant_pairs=[("s1", "s2", 0.85)],
            complementary_pairs=[],
            independence_scores={},
            recommendations=[],
        )
        # 引入第三个独立策略，才能观察到冗余对的相对降权
        base_weights = {"s1": 0.3, "s2": 0.3, "s3": 0.4}
        recommended = recommend_weights(result, base_weights)

        assert recommended["s1"] < 0.3
        assert recommended["s2"] < 0.3
        assert recommended["s3"] > 0.4
        assert sum(recommended.values()) == pytest.approx(1.0)
