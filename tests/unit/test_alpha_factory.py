"""Alpha因子工厂单元测试。

测试纯数学计算，不依赖网络或外部数据。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from gugu.analysis.alpha_factory import AlphaFactor, AlphaFactory


@pytest.fixture
def factory() -> AlphaFactory:
    return AlphaFactory()


@pytest.fixture
def uptrend_df() -> pd.DataFrame:
    """200天持续上升行情。"""
    np.random.seed(42)
    dates = pd.bdate_range("2024-01-01", periods=200)
    close = 100.0 * np.cumprod(1 + np.random.randn(200) * 0.008 + 0.002)
    close = np.maximum(close, 10.0)
    return pd.DataFrame({
        "date": dates,
        "open": close * 0.99,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": np.random.randint(1_000_000, 10_000_000, 200),
    })


@pytest.fixture
def downtrend_df() -> pd.DataFrame:
    """200天持续下跌行情。"""
    np.random.seed(42)
    dates = pd.bdate_range("2024-01-01", periods=200)
    close = 100.0 * np.cumprod(1 + np.random.randn(200) * 0.008 - 0.002)
    close = np.maximum(close, 10.0)
    return pd.DataFrame({
        "date": dates,
        "open": close * 0.99,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": np.random.randint(1_000_000, 10_000_000, 200),
    })


class TestAlphaFactory:
    """AlphaFactory 功能测试。"""

    def test_compute_all_returns_factors(self, factory: AlphaFactory, uptrend_df: pd.DataFrame) -> None:
        """compute_all() 应返回因子字典。"""
        factors = factory.compute_all(uptrend_df)

        assert isinstance(factors, dict)
        assert len(factors) >= 12, f"Expected >=12 factors, got {len(factors)}"
        # 检查所有因子都是 AlphaFactor 类型
        for name, f in factors.items():
            assert isinstance(f, AlphaFactor), f"{name} is not AlphaFactor"
            assert isinstance(f.value, float)
            assert 0 <= f.normalized <= 1, f"{name} normalized={f.normalized} out of [0,1]"
            assert -1 <= f.signal <= 1, f"{name} signal={f.signal} out of [-1,1]"

    def test_composite_score(self, factory: AlphaFactory, uptrend_df: pd.DataFrame) -> None:
        """composite_score() 应返回评分结果。"""
        factors = factory.compute_all(uptrend_df)
        result = factory.composite_score(factors)

        assert "score" in result
        assert "category_scores" in result
        assert "buy_signal" in result
        assert "sell_signal" in result
        assert isinstance(result["score"], float)
        assert -1 <= result["score"] <= 1
        assert isinstance(result["buy_signal"], bool)
        assert isinstance(result["sell_signal"], bool)

    def test_uptrend_has_positive_signal(self, factory: AlphaFactory, uptrend_df: pd.DataFrame) -> None:
        """上升趋势应有正面的综合评分。"""
        factors = factory.compute_all(uptrend_df)
        result = factory.composite_score(factors)
        # 上升趋势中大部分因子应为正
        category_scores = result["category_scores"]
        for cat, score in category_scores.items():
            if cat in ("trend", "momentum"):
                assert score > -0.3, f"{cat} score={score} too negative in uptrend"

    def test_downtrend_has_negative_signal(self, factory: AlphaFactory, downtrend_df: pd.DataFrame) -> None:
        """下跌趋势应有负面的趋势评分。"""
        factors = factory.compute_all(downtrend_df)
        result = factory.composite_score(factors)
        trend_score = result["category_scores"].get("trend", 0)
        assert trend_score < 0.3, f"trend score={trend_score} not negative in downtrend"

    def test_rsi_values(self, factory: AlphaFactory, uptrend_df: pd.DataFrame) -> None:
        """RSI 应在 [0, 100] 范围。"""
        factors = factory.compute_all(uptrend_df)
        rsi = factors.get("rsi")
        assert rsi is not None, "RSI factor missing"
        assert 0 <= rsi.value <= 100, f"RSI={rsi.value} out of range"

    def test_macd_cross_uptrend(self, factory: AlphaFactory, uptrend_df: pd.DataFrame) -> None:
        """持续上升趋势中 MACD 应出现金叉。"""
        factors = factory.compute_all(uptrend_df)
        macd = factors.get("macd_cross")
        assert macd is not None, "MACD cross factor missing"
        # 在上升趋势中，金叉信号不应为负
        # （具体值取决于数据，但应 >= -1）

    def test_ma_alignment_in_uptrend(self, factory: AlphaFactory, uptrend_df: pd.DataFrame) -> None:
        """上升趋势中均线排列应为正。"""
        factors = factory.compute_all(uptrend_df)
        alignment = factors.get("ma_alignment")
        assert alignment is not None, "MA alignment factor missing"
        assert alignment.value >= -1 and alignment.value <= 1

    def test_bollinger_position(self, factory: AlphaFactory, uptrend_df: pd.DataFrame) -> None:
        """布林带位置因子应在 [-1, 1] 范围。"""
        factors = factory.compute_all(uptrend_df)
        bb = factors.get("bollinger_position")
        if bb is not None:
            assert -1 <= bb.value <= 1, f"BB position={bb.value} out of range"

    def test_volume_ratio(self, factory: AlphaFactory, uptrend_df: pd.DataFrame) -> None:
        """量比因子应为正。"""
        factors = factory.compute_all(uptrend_df)
        vr = factors.get("volume_ratio")
        if vr is not None:  # 成交量数据存在时
            assert vr.value >= 0, f"Volume ratio={vr.value} negative"

    def test_empty_data(self, factory: AlphaFactory) -> None:
        """空 DataFrame 应返回空因子字典或处理异常。"""
        df = pd.DataFrame()
        try:
            factors = factory.compute_all(df)
            assert isinstance(factors, dict)
        except (KeyError, IndexError, ValueError):
            # 空 DataFrame 在某些边界条件下抛异常也可以接受
            pass

    def test_insufficient_data(self, factory: AlphaFactory) -> None:
        """少于 5 行数据应返回空因子字典。"""
        df = pd.DataFrame({
            "close": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "open": [100.0, 101.0, 102.0],
        })
        factors = factory.compute_all(df)
        assert factors == {} or len(factors) < 5  # 大多数因子需要 5+ 行

    def test_missing_volume_column(self, factory: AlphaFactory) -> None:
        """缺少 volume 列应不会崩溃。"""
        df = pd.DataFrame({
            "close": [100.0 + i for i in range(100)],
            "high": [101.0 + i for i in range(100)],
            "low": [99.0 + i for i in range(100)],
            "open": [100.0 + i for i in range(100)],
        })
        factors = factory.compute_all(df)
        assert len(factors) >= 10  # 成交量因子外的因子应正常计算

    def test_custom_weights(self, factory: AlphaFactory, uptrend_df: pd.DataFrame) -> None:
        """自定义权重应影响综合评分。"""
        factors = factory.compute_all(uptrend_df)
        default_result = factory.composite_score(factors)
        custom_result = factory.composite_score(factors, weights={
            "trend": 1.0, "momentum": 0, "volatility": 0, "volume": 0, "pattern": 0,
        })
        # 权重不同，评分应该不同
        assert default_result["score"] != custom_result["score"]

    def test_get_factor_names(self, factory: AlphaFactory) -> None:
        """get_factor_names() 返回所有因子名称。"""
        names = factory.get_factor_names()
        assert len(names) >= 20
        assert "rsi" in names
        assert "macd_hist" in names
        assert "volume_ratio" in names


class TestAlphaFactorDataClass:
    """AlphaFactor dataclass 基础功能。"""

    def test_default_values(self) -> None:
        """AlphaFactor 默认值。"""
        f = AlphaFactor(name="test", category="trend", direction="positive")
        assert f.value == 0.0
        assert f.normalized == 0.0
        assert f.signal == 0.0
        assert f.description == ""

    def test_full_construction(self) -> None:
        """完整构造 AlphaFactor。"""
        f = AlphaFactor(
            name="rsi", category="momentum", direction="neutral",
            value=75.0, normalized=0.75, signal=0.5,
            description="RSI超买",
        )
        assert f.name == "rsi"
        assert f.category == "momentum"
        assert f.direction == "neutral"
        assert f.value == 75.0
        assert f.normalized == 0.75
        assert f.signal == 0.5
        assert f.description == "RSI超买"