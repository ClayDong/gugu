"""量价背离检测器 DivergenceDetector 单元测试。"""

from __future__ import annotations

import pandas as pd

from gugu.detectors.divergence_detector import DivergenceDetector


def _make_df(n: int = 60, base_price: float = 100.0, base_vol: float = 1_000_000) -> pd.DataFrame:
    """构造测试用 K 线数据。"""
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "open": [base_price] * n,
            "high": [base_price + 2] * n,
            "low": [base_price - 2] * n,
            "close": [base_price] * n,
            "volume": [base_vol] * n,
        }
    )


class TestVolumePriceDivergence:
    """量价背离检测测试。"""

    def test_no_divergence_in_normal_data(self):
        """正常数据无背离信号。"""
        df = _make_df(60)
        detector = DivergenceDetector()
        result = detector.detect_volume_price_divergence(df)
        assert result.iloc[-1] == 0

    def test_divergence_when_price_new_high_volume_shrinks(self):
        """价格创新高但成交量萎缩时检测到背离。"""
        df = _make_df(60, base_price=100.0, base_vol=1_000_000)
        # 最后 20 天价格创新高
        df.loc[40:, "close"] = 110.0
        df.loc[59, "close"] = 120.0  # 最后一天创新高
        # 最后 5 天成交量萎缩
        df.loc[55:, "volume"] = 500_000
        detector = DivergenceDetector()
        result = detector.detect_volume_price_divergence(df)
        assert result.iloc[-1] == 1

    def test_no_divergence_when_volume_stays_high(self):
        """价格创新高且成交量同步放大时无背离。"""
        df = _make_df(60, base_price=100.0, base_vol=1_000_000)
        df.loc[40:, "close"] = 110.0
        df.loc[59, "close"] = 120.0
        # 成交量同步放大
        df.loc[55:, "volume"] = 2_000_000
        detector = DivergenceDetector()
        result = detector.detect_volume_price_divergence(df)
        assert result.iloc[-1] == 0


class TestClimaxVolume:
    """天量滞涨检测测试。"""

    def test_no_climax_in_normal_data(self):
        """正常数据无天量滞涨。"""
        df = _make_df(60)
        detector = DivergenceDetector()
        result = detector.detect_climax_volume(df)
        assert result.iloc[-1] == 0

    def test_climax_when_volume_spike_and_price_flat(self):
        """成交量激增但价格不变时检测到天量滞涨。"""
        df = _make_df(60, base_price=100.0, base_vol=1_000_000)
        # 最后一天成交量激增
        df.loc[59, "volume"] = 3_000_000
        # 价格变化 < 1%
        df.loc[59, "close"] = 100.5
        detector = DivergenceDetector()
        result = detector.detect_climax_volume(df)
        assert result.iloc[-1] == 1

    def test_no_climax_when_price_moves_significantly(self):
        """价格大幅变动时不视为滞涨。"""
        df = _make_df(60, base_price=100.0, base_vol=1_000_000)
        df.loc[59, "volume"] = 3_000_000
        df.loc[59, "close"] = 105.0  # 5% 涨幅
        detector = DivergenceDetector()
        result = detector.detect_climax_volume(df)
        assert result.iloc[-1] == 0


class TestDetect:
    """综合检测测试。"""

    def test_detect_returns_correct_structure(self):
        """detect 返回正确的数据结构。"""
        df = _make_df(60)
        detector = DivergenceDetector()
        result = detector.detect(df)
        assert "divergence" in result
        assert "climax_volume" in result
        assert "signals" in result
        assert isinstance(result["signals"], list)

    def test_detect_with_insufficient_data(self):
        """数据不足时返回空结果。"""
        df = _make_df(10)
        detector = DivergenceDetector()
        result = detector.detect(df, window=20)
        assert result["divergence"] is False
        assert result["climax_volume"] is False
        assert result["signals"] == []

    def test_detect_finds_divergence_signal(self):
        """量价背离时 detect 返回背离信号。"""
        df = _make_df(60, base_price=100.0, base_vol=1_000_000)
        # 价格创新高
        df.loc[40:, "close"] = 110.0
        df.loc[59, "close"] = 120.0
        # 成交量萎缩（背离）
        df.loc[55:, "volume"] = 500_000
        detector = DivergenceDetector()
        result = detector.detect(df)
        assert result["divergence"] is True
        assert any("背离" in s for s in result["signals"])

    def test_detect_finds_climax_signal(self):
        """天量滞涨时 detect 返回天量滞涨信号。"""
        df = _make_df(60, base_price=100.0, base_vol=1_000_000)
        # 最后一天成交量激增但价格几乎不动
        df.loc[59, "volume"] = 3_000_000
        df.loc[59, "close"] = 100.5  # 涨幅 < 1%
        detector = DivergenceDetector()
        result = detector.detect(df)
        assert result["climax_volume"] is True
        assert any("天量" in s for s in result["signals"])
