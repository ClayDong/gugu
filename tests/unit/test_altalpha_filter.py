"""单元测试：AltAlphaFilter 另类 Alpha 因子过滤器。

覆盖：
- 高评分时 confidence 提升
- 低评分时标记过滤
- 数据缺失时默认放行
- 缓存命中
- 配置阈值读取
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gugu.filters.altalpha import AltAlphaFilter


class TestAltAlphaFilter:
    """AltAlphaFilter 单元测试。"""

    @pytest.fixture
    def filter(self) -> AltAlphaFilter:
        """创建过滤器实例并清理缓存。"""
        f = AltAlphaFilter()
        f._cache.clear()
        return f

    @pytest.mark.asyncio
    async def test_high_score_boosts_confidence(self, filter: AltAlphaFilter) -> None:
        """综合评分高于 boost_threshold → pass=True 且 confidence_boost > 0。"""
        with (
            patch.object(filter, "_get_engine") as mock_get_engine,
            patch.object(filter._factory, "compute_altalpha_factors") as mock_compute_factors,
            patch.object(filter._factory, "composite_score_with_altalpha") as mock_composite,
        ):
            mock_engine = MagicMock()
            mock_engine.compute_all = AsyncMock(return_value={"600519": {}})
            mock_engine.compute_single = MagicMock(return_value={"insider_buy": 1.5})
            mock_get_engine.return_value = mock_engine

            mock_compute_factors.return_value = {"insider_buy": MagicMock(signal=0.5)}
            mock_composite.return_value = {
                "score": 0.45,
                "altalpha_score": 0.45,
                "tech_score": 0.0,
                "top_factors": [("insider_buy", 0.5, "高管净增持")],
            }

            result = await filter.check("600519")

        assert result["pass"] is True
        assert result["combined_score"] == 0.45
        assert result["confidence_boost"] == 0.10
        assert "另类因子综合评分 0.45 > 0.30" in result["reasons"][0]

    @pytest.mark.asyncio
    async def test_low_score_blocks_signal(self, filter: AltAlphaFilter) -> None:
        """综合评分低于 block_threshold → pass=False。"""
        with (
            patch.object(filter, "_get_engine") as mock_get_engine,
            patch.object(filter._factory, "compute_altalpha_factors") as mock_compute_factors,
            patch.object(filter._factory, "composite_score_with_altalpha") as mock_composite,
        ):
            mock_engine = MagicMock()
            mock_engine.compute_all = AsyncMock(return_value={"600519": {}})
            mock_engine.compute_single = MagicMock(return_value={"margin_sentiment": -1.8})
            mock_get_engine.return_value = mock_engine

            mock_compute_factors.return_value = {"margin_sentiment": MagicMock(signal=-0.6)}
            mock_composite.return_value = {
                "score": -0.60,
                "altalpha_score": -0.60,
                "tech_score": 0.0,
                "top_factors": [("margin_sentiment", -0.6, "融资余额变化")],
            }

            result = await filter.check("600519")

        assert result["pass"] is False
        assert result["combined_score"] == -0.60
        assert "事件面偏弱" in result["reasons"][0]

    @pytest.mark.asyncio
    async def test_missing_data_defaults_to_pass(self, filter: AltAlphaFilter) -> None:
        """因子数据全为 0 时默认放行并标记 data_missing。"""
        with patch.object(filter, "_get_engine") as mock_get_engine:
            mock_engine = MagicMock()
            mock_engine.compute_all = AsyncMock(return_value={"600519": {}})
            mock_engine.compute_single = MagicMock(
                return_value={
                    "insider_buy": 0.0,
                    "lhb_inst_net": 0.0,
                    "margin_sentiment": 0.0,
                }
            )
            mock_get_engine.return_value = mock_engine

            result = await filter.check("600519")

        assert result["pass"] is True
        assert result["data_missing"] is True
        assert result["reasons"] == ["另类因子数据不可用"]

    @pytest.mark.asyncio
    async def test_engine_exception_defaults_to_pass(self, filter: AltAlphaFilter) -> None:
        """引擎异常时默认放行。"""
        with patch.object(filter, "_get_engine") as mock_get_engine:
            mock_engine = MagicMock()
            mock_engine.compute_all = AsyncMock(side_effect=Exception("API error"))
            mock_get_engine.return_value = mock_engine

            result = await filter.check("600519")

        assert result["pass"] is True
        assert result["data_missing"] is True

    @pytest.mark.asyncio
    async def test_caching_same_day(self, filter: AltAlphaFilter) -> None:
        """当日缓存命中时不再调用引擎。"""
        cached_result = {
            "pass": True,
            "altalpha_score": 0.5,
            "combined_score": 0.5,
            "tech_score": 0.0,
            "top_factors": [],
            "confidence_boost": 0.10,
            "reasons": ["cached"],
            "data_missing": False,
        }
        filter._cache["600519"] = (date.today(), cached_result)

        with patch.object(filter, "_get_engine") as mock_get_engine:
            result = await filter.check("600519")
            mock_get_engine.assert_not_called()

        assert result == cached_result

    @pytest.mark.asyncio
    async def test_cache_expires_next_day(self, filter: AltAlphaFilter) -> None:
        """跨日缓存自动失效并重新计算。"""
        filter._cache["600519"] = (
            date(2020, 1, 1),
            {"pass": True, "reasons": ["old"], "data_missing": False},
        )

        with patch.object(filter, "_get_engine") as mock_get_engine:
            mock_engine = MagicMock()
            mock_engine.compute_all = AsyncMock(return_value={"600519": {}})
            mock_engine.compute_single = MagicMock(return_value={"insider_buy": 0.0})
            mock_get_engine.return_value = mock_engine

            result = await filter.check("600519")

        assert result["data_missing"] is True

    def test_load_thresholds_from_config(self, filter: AltAlphaFilter) -> None:
        """配置覆盖默认阈值。"""
        fake_yaml = {
            "altalpha_filter": {
                "block_threshold": -0.80,
                "boost_threshold": 0.50,
                "confidence_boost": 0.20,
                "altalpha_weight": 0.50,
                "fetch_financial": True,
            }
        }
        with patch("gugu._legacy_config.load_yaml", return_value=fake_yaml):
            thresholds = filter._load_thresholds()

        assert thresholds["block_threshold"] == -0.80
        assert thresholds["boost_threshold"] == 0.50
        assert thresholds["confidence_boost"] == 0.20
        assert thresholds["altalpha_weight"] == 0.50
        assert thresholds["fetch_financial"] is True

    def test_load_thresholds_defaults_when_config_missing(self, filter: AltAlphaFilter) -> None:
        """配置缺失时使用默认值。"""
        with patch("gugu._legacy_config.load_yaml", side_effect=Exception("no config")):
            thresholds = filter._load_thresholds()

        assert thresholds["block_threshold"] == -0.50
        assert thresholds["boost_threshold"] == 0.30
        assert thresholds["confidence_boost"] == 0.10
        assert thresholds["altalpha_weight"] == 0.30
        assert thresholds["fetch_financial"] is False
