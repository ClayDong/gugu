"""ML 因子模型测试：IC 分析 + 正交化 + LightGBM 训练/预测 + 策略信号生成。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

_requires_sklearn = pytest.importorskip("sklearn", reason="sklearn not installed")


@pytest.fixture
def sample_ohlcv() -> pd.DataFrame:
    """生成 200 行模拟 OHLCV 数据。"""
    np.random.seed(42)
    n = 200
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = 10.0 + np.cumsum(np.random.randn(n) * 0.2)
    close = np.maximum(close, 1.0)
    high = close * (1 + np.abs(np.random.randn(n)) * 0.01)
    low = close * (1 - np.abs(np.random.randn(n)) * 0.01)
    open_ = close + np.random.randn(n) * 0.05
    volume = np.random.randint(100000, 500000, n).astype(float)
    amount = volume * close

    return pd.DataFrame(
        {
            "date": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )


@pytest.fixture
def sample_factor_df(sample_ohlcv: pd.DataFrame) -> pd.DataFrame:
    """生成因子矩阵。"""
    from gugu.analysis.alpha_factory import AlphaFactory

    factory = AlphaFactory()
    rows: list[dict[str, float]] = []
    for i in range(len(sample_ohlcv)):
        chunk = sample_ohlcv.iloc[: i + 1]
        if len(chunk) < 30:
            rows.append({})
            continue
        factors = factory.compute_all(chunk)
        row = {name: f.value for name, f in factors.items()}
        rows.append(row)

    df = pd.DataFrame(rows)
    df["close"] = sample_ohlcv["close"].values
    return df


class TestFactorICAnalyzer:
    """因子 IC 分析器测试。"""

    def test_compute_forward_returns(self, sample_ohlcv: pd.DataFrame) -> None:
        """测试未来收益计算。"""
        from gugu.analysis.factor_ic import FactorICAnalyzer

        analyzer = FactorICAnalyzer(forward_days=5)
        ret = analyzer.compute_forward_returns(sample_ohlcv["close"])

        assert len(ret) == len(sample_ohlcv)
        # 最后 5 个应该是 NaN
        assert ret.iloc[-1] != ret.iloc[-1]  # NaN check
        # 倒数第 6 个应该有值
        assert not np.isnan(ret.iloc[-6])

    def test_compute_ic(self) -> None:
        """测试 IC 计算。"""
        from gugu.analysis.factor_ic import FactorICAnalyzer

        analyzer = FactorICAnalyzer()
        factor = pd.Series(np.random.randn(100))
        returns = pd.Series(np.random.randn(100))
        ic = analyzer.compute_ic(factor, returns)
        assert -1 <= ic <= 1

    def test_compute_ic_insufficient_samples(self) -> None:
        """样本不足时 IC 返回 0。"""
        from gugu.analysis.factor_ic import FactorICAnalyzer

        analyzer = FactorICAnalyzer()
        factor = pd.Series([1.0, 2.0, 3.0])
        returns = pd.Series([0.1, 0.2, 0.3])
        ic = analyzer.compute_ic(factor, returns)
        assert ic == 0.0

    def test_analyze_multiple_factors(self, sample_factor_df: pd.DataFrame) -> None:
        """测试多因子 IC 分析。"""
        from gugu.analysis.factor_ic import FactorICAnalyzer

        factor_cols = [c for c in sample_factor_df.columns if c != "close"]
        if len(factor_cols) < 2:
            pytest.skip("因子列不足")

        analyzer = FactorICAnalyzer(forward_days=5)
        results = analyzer.analyze(sample_factor_df, factor_cols[:5])

        assert len(results) <= 5
        for name, r in results.items():
            assert r.factor_name == name
            assert -1 <= r.ic_mean <= 1
            assert r.rank >= 1

    def test_get_effective_factors(self, sample_factor_df: pd.DataFrame) -> None:
        """测试有效因子筛选。"""
        from gugu.analysis.factor_ic import FactorICAnalyzer

        factor_cols = [c for c in sample_factor_df.columns if c != "close"]
        if len(factor_cols) < 2:
            pytest.skip("因子列不足")

        analyzer = FactorICAnalyzer(forward_days=5)
        results = analyzer.analyze(sample_factor_df, factor_cols[:5])
        effective = analyzer.get_effective_factors(results, ic_threshold=0.0, ir_threshold=0.0)

        # 阈值为 0 时所有因子都应有效
        assert len(effective) == len(results)

    def test_summary(self, sample_factor_df: pd.DataFrame) -> None:
        """测试分析摘要。"""
        from gugu.analysis.factor_ic import FactorICAnalyzer

        factor_cols = [c for c in sample_factor_df.columns if c != "close"]
        if len(factor_cols) < 2:
            pytest.skip("因子列不足")

        analyzer = FactorICAnalyzer(forward_days=5)
        results = analyzer.analyze(sample_factor_df, factor_cols[:5])
        analyzer.get_effective_factors(results, ic_threshold=0.0, ir_threshold=0.0)
        s = analyzer.summary(results)

        assert s["total_factors"] == len(results)
        assert s["effective_count"] >= 0
        assert s["ineffective_count"] >= 0
        assert s["forward_days"] == 5
        assert len(s["top_5_by_ic"]) <= 5


class TestFactorOrthogonalizer:
    """因子正交化器测试。"""

    def test_fit_transform(self, sample_factor_df: pd.DataFrame) -> None:
        """测试拟合并转换。"""
        from gugu.analysis.ml_factor import FactorOrthogonalizer

        factor_cols = [c for c in sample_factor_df.columns if c != "close"]
        if len(factor_cols) < 3:
            pytest.skip("因子列不足")

        data = sample_factor_df[factor_cols].dropna()
        if len(data) < 50:
            pytest.skip("有效样本不足")

        orth = FactorOrthogonalizer(n_components=3)
        result = orth.fit_transform(data)

        assert result.shape[1] == 3
        assert orth.n_kept == 3

    def test_transform_without_fit(self) -> None:
        """未拟合时 transform 返回原始数据。"""
        from gugu.analysis.ml_factor import FactorOrthogonalizer

        orth = FactorOrthogonalizer()
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        result = orth.transform(df)
        assert result.shape == (3, 2)

    def test_insufficient_samples(self) -> None:
        """样本不足时跳过正交化。"""
        from gugu.analysis.ml_factor import FactorOrthogonalizer

        df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        orth = FactorOrthogonalizer()
        orth.fit(df)

        assert orth.scaler is None
        assert orth.pca is None
        assert orth.n_kept == 2


class TestMLFactorModel:
    """LightGBM 多因子模型测试。"""

    def test_train_and_predict(self, sample_factor_df: pd.DataFrame) -> None:
        """测试训练和预测。"""
        from gugu.analysis.ml_factor import MLFactorModel

        factor_cols = [c for c in sample_factor_df.columns if c != "close"]
        if len(factor_cols) < 3:
            pytest.skip("因子列不足")

        model = MLFactorModel(forward_days=5)
        result = model.train(sample_factor_df, sample_factor_df["close"], factor_cols)

        if result.get("status") != "trained":
            pytest.skip(f"训练条件不满足: {result}")

        assert model.is_trained
        assert result["train_accuracy"] > 0.0

        # 预测
        row = sample_factor_df[factor_cols].iloc[-1].to_dict()
        score = model.predict(row)
        assert 0.0 <= score <= 1.0

    def test_predict_untrained(self) -> None:
        """未训练模型预测返回 0.5。"""
        from gugu.analysis.ml_factor import MLFactorModel

        model = MLFactorModel()
        score = model.predict({"a": 1.0, "b": 2.0})
        assert score == 0.5

    def test_predict_batch(self, sample_factor_df: pd.DataFrame) -> None:
        """测试批量预测。"""
        from gugu.analysis.ml_factor import MLFactorModel

        factor_cols = [c for c in sample_factor_df.columns if c != "close"]
        if len(factor_cols) < 3:
            pytest.skip("因子列不足")

        model = MLFactorModel(forward_days=5)
        result = model.train(sample_factor_df, sample_factor_df["close"], factor_cols)

        if result.get("status") != "trained":
            pytest.skip(f"训练条件不满足: {result}")

        data = sample_factor_df[factor_cols].dropna()
        scores = model.predict_batch(data)
        assert len(scores) == len(data)
        assert np.all((scores >= 0) & (scores <= 1))

    def test_save_and_load(self, sample_factor_df: pd.DataFrame, tmp_path) -> None:
        """测试模型保存和加载。"""
        from gugu.analysis.ml_factor import MLFactorModel

        factor_cols = [c for c in sample_factor_df.columns if c != "close"]
        if len(factor_cols) < 3:
            pytest.skip("因子列不足")

        model = MLFactorModel(forward_days=5)
        result = model.train(sample_factor_df, sample_factor_df["close"], factor_cols)

        if result.get("status") != "trained":
            pytest.skip(f"训练条件不满足: {result}")

        # 保存
        save_path = tmp_path / "model.pkl"
        model.save(save_path)
        assert save_path.exists()

        # 加载
        model2 = MLFactorModel()
        loaded = model2.load(save_path)
        assert loaded
        assert model2.is_trained

        # 预测结果应一致
        row = sample_factor_df[factor_cols].iloc[-1].to_dict()
        score1 = model.predict(row)
        score2 = model2.predict(row)
        assert abs(score1 - score2) < 0.01

    def test_get_feature_importance(self, sample_factor_df: pd.DataFrame) -> None:
        """测试特征重要性。"""
        from gugu.analysis.ml_factor import MLFactorModel

        factor_cols = [c for c in sample_factor_df.columns if c != "close"]
        if len(factor_cols) < 3:
            pytest.skip("因子列不足")

        model = MLFactorModel(forward_days=5)
        result = model.train(sample_factor_df, sample_factor_df["close"], factor_cols)

        if result.get("status") != "trained":
            pytest.skip(f"训练条件不满足: {result}")

        importance = model.get_feature_importance()
        assert len(importance) > 0
        assert all(v >= 0 for v in importance.values())


class TestMLFactorResearchBoundary:
    """已归档 ML 策略不再作为运行时策略测试。"""

    def test_strategy_is_not_in_runtime_registry(self) -> None:
        """ML 因子研究代码与生产策略注册表保持隔离。"""
        from gugu.strategies.registry import list_strategies

        assert "ml_factor" not in list_strategies()
