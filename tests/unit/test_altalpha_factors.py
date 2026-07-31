"""另类Alpha因子系统单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from gugu.analysis.altalpha_factors import (
    AltAlphaFactorEngine,
    FACTOR_NAMES,
    _zscore,
)
from gugu.data.collectors.altalpha.base import AltAlphaCollector


class TestZscore:
    """_zscore 函数测试。"""

    def test_basic(self):
        s = pd.Series([1, 2, 3, 4, 5])
        z = _zscore(s)
        assert z.mean() == pytest.approx(0, abs=1e-10)
        assert len(z) == 5

    def test_clip(self):
        s = pd.Series([1, 2, 3, 4, 100])
        z = _zscore(s, clip=3.0)
        assert z.max() <= 3.0
        assert z.min() >= -3.0

    def test_fillna(self):
        s = pd.Series([1, np.nan, 3])
        z = _zscore(s)
        assert z.iloc[1] == 0.0

    def test_constant(self):
        s = pd.Series([5, 5, 5, 5])
        z = _zscore(s)
        assert (z == 0.0).all()


class TestAltAlphaCollector:
    """AltAlphaCollector 基类测试。"""

    def test_init_creates_cache_dir(self, tmp_path):
        collector = AltAlphaCollector(cache_dir=str(tmp_path / "test_cache"))
        assert (tmp_path / "test_cache").exists()

    def test_cache_write_read(self, tmp_path):
        collector = AltAlphaCollector(cache_dir=str(tmp_path))
        collector._data_type = "test"

        df = pd.DataFrame({"code": ["600519"], "value": [1.0]})
        collector._write_cache(df)

        cached = collector._read_cache()
        assert cached is not None
        assert len(cached) == 1

    def test_cache_miss(self, tmp_path):
        collector = AltAlphaCollector(cache_dir=str(tmp_path))
        collector._data_type = "nonexist"
        cached = collector._read_cache()
        assert cached is None

    def test_cache_ignore_ttl(self, tmp_path):
        collector = AltAlphaCollector(cache_dir=str(tmp_path))
        collector._data_type = "test"

        df = pd.DataFrame({"code": ["600519"], "value": [1.0]})
        collector._write_cache(df)

        cached = collector._read_cache(ignore_ttl=True)
        assert cached is not None

    def test_cache_key_isolation(self, tmp_path):
        """不同参数应写入不同缓存键，避免共享缓存。"""
        collector = AltAlphaCollector(cache_dir=str(tmp_path))
        collector._data_type = "test"

        df_a = pd.DataFrame({"code": ["600519"], "value": [1.0]})
        df_b = pd.DataFrame({"code": ["000001"], "value": [2.0]})

        key_a = collector._build_cache_key(lambda: None, symbol="600519")  # type: ignore[return]
        key_b = collector._build_cache_key(lambda: None, symbol="000001")  # type: ignore[return]

        collector._write_cache(df_a, cache_key=key_a)
        collector._write_cache(df_b, cache_key=key_b)

        cached_a = collector._read_cache(cache_key=key_a)
        cached_b = collector._read_cache(cache_key=key_b)

        assert cached_a is not None and cached_a["code"].iloc[0] == "600519"
        assert cached_b is not None and cached_b["code"].iloc[0] == "000001"


class TestInsiderBuyFactor:
    """高管增持因子测试。"""

    def test_compute_insider_buy(self):
        engine = AltAlphaFactorEngine()
        symbols = ["600519", "000858", "300750"]

        df = pd.DataFrame(
            {
                "code": ["600519", "600519", "000858", "300750"],
                "change_shares": [10000, -5000, 20000, -3000],
                "avg_price": [1800, 1750, 150, 100],
            }
        )

        result = engine._compute_insider_buy(df, symbols)
        assert len(result) == 3
        # 600519: 10000*1800 - 5000*1750 = 18M - 8.75M = 9.25M (net buy)
        # 000858: 20000*150 = 3M (net buy)
        # 300750: -3000*100 = -0.3M (net sell)
        # 600519 should have highest z-score
        assert result["600519"] > result["300750"]

    def test_empty_data(self):
        engine = AltAlphaFactorEngine()
        result = engine._compute_insider_buy(pd.DataFrame(), ["600519"])
        assert len(result) == 1
        assert result["600519"] == 0.0


class TestEarningsSurpriseFactor:
    """业绩预告因子测试。"""

    def test_compute_earnings_surprise(self):
        engine = AltAlphaFactorEngine()
        symbols = ["600519", "000858"]

        df = pd.DataFrame(
            {
                "code": ["600519", "000858"],
                "growth": [30.0, -20.0],  # 业绩变动幅度（%）
                "forecast_type": ["预增", "预减"],
            }
        )

        result = engine._compute_earnings_surprise(df, symbols)
        assert len(result) == 2
        # 600519 预增 → positive
        assert result["600519"] > result["000858"]


class TestBlockDiscountFactor:
    """大宗交易活跃度因子测试。"""

    def test_compute_block_discount(self):
        engine = AltAlphaFactorEngine()
        symbols = ["600519", "000858"]

        df = pd.DataFrame(
            {
                "code": ["600519", "000858"],
                "amount": [1e8, 1e7],  # 600519大宗交易额更大
            }
        )

        result = engine._compute_block_discount(df, symbols)
        assert len(result) == 2
        # 600519 成交额更大 → 活跃度更高 → factor 更大
        assert result["600519"] > result["000858"]


class TestUnlockPressureFactor:
    """解禁压力因子测试。"""

    def test_compute_unlock_pressure(self):
        engine = AltAlphaFactorEngine()
        symbols = ["600519", "000858", "300750"]

        # 用15天后作为解禁日期（在30天窗口内）
        from datetime import datetime, timedelta

        future_date = (datetime.now() + timedelta(days=15)).strftime("%Y-%m-%d")
        df = pd.DataFrame(
            {
                "code": ["600519", "000858", "300750"],
                "unlock_date": [future_date] * 3,
                "unlock_value": [1e9, 0, 5e8],  # 600519解禁最大
            }
        )

        result = engine._compute_unlock_pressure(df, symbols)
        assert len(result) == 3
        # 600519 解禁最大 → factor 最小（看空）
        assert result["600519"] < result["000858"]


class TestAltAlphaFactorEngine:
    """AltAlphaFactorEngine 集成测试。"""

    @pytest.mark.asyncio
    async def test_compute_all_empty_symbols(self):
        engine = AltAlphaFactorEngine()
        result = await engine.compute_all([])
        assert result == {}

    @pytest.mark.asyncio
    async def test_compute_all_returns_all_factors(self):
        engine = AltAlphaFactorEngine()
        symbols = ["600519", "000858"]

        # Mock 所有采集器返回空 DataFrame
        with patch.object(engine, "_get_insider_collector") as mock_insider:
            mock_collector = MagicMock()
            mock_collector.fetch_recent = AsyncMock(return_value=pd.DataFrame())
            mock_insider.return_value = mock_collector

            with patch.object(engine, "_get_earnings_collector") as mock_earnings:
                mock_ec = MagicMock()
                mock_ec.fetch_latest = AsyncMock(return_value=pd.DataFrame())
                mock_earnings.return_value = mock_ec

                with patch.object(engine, "_get_lhb_collector") as mock_lhb:
                    mock_lc = MagicMock()
                    mock_lc.fetch_detail = AsyncMock(return_value=pd.DataFrame())
                    mock_lhb.return_value = mock_lc

                    with (
                        patch.object(engine, "_get_block_collector") as mock_block,
                        patch.object(engine, "_get_margin_collector") as mock_margin,
                        patch.object(engine, "_get_unlock_collector") as mock_unlock,
                        patch.object(engine, "_compute_holder_concentrate") as mock_holder,
                    ):
                        mock_block.return_value.fetch_detail = AsyncMock(
                            return_value=pd.DataFrame()
                        )
                        mock_margin.return_value.fetch_detail = AsyncMock(
                            return_value=pd.DataFrame()
                        )
                        mock_unlock.return_value.fetch_upcoming = AsyncMock(
                            return_value=pd.DataFrame()
                        )
                        mock_holder.return_value = pd.Series(0.0, index=symbols)

                        result = await engine.compute_all(symbols)

        assert "600519" in result
        assert "000858" in result
        assert all(f in result["600519"] for f in FACTOR_NAMES)

    def test_compute_single(self):
        engine = AltAlphaFactorEngine()
        all_factors = {
            "600519": {"insider_buy": 1.5, "lhb_inst_net": -0.3},
            "000858": {"insider_buy": 0.2, "lhb_inst_net": 0.8},
        }
        result = engine.compute_single("600519", all_factors)
        assert result["insider_buy"] == 1.5
        assert result["lhb_inst_net"] == -0.3
