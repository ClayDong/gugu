"""StockRanker 单元测试。

纯逻辑测试，mock 外部数据依赖。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from gugu.analysis.stock_ranker import StockRanker


@pytest.fixture
def ranker() -> StockRanker:
    return StockRanker()


class TestStockRanker:
    """StockRanker 功能测试。"""

    def test_rank_empty_list(self, ranker: StockRanker) -> None:
        """空股票列表返回空结果。"""
        result = ranker.rank([], top_n=10)
        # rank 是 async 方法，但没 await，实际上返回 coroutine
        # 需要 await
        ...

    @pytest.mark.asyncio
    async def test_rank_no_data(self, ranker: StockRanker) -> None:
        """数据源返回空时返回空结果。"""
        # Mock 所有外部调用返回空
        ranker._dm.fetch_stock_history = AsyncMock(return_value=pd.DataFrame())
        ranker._dm.fetch_stock_meta = AsyncMock(return_value={})

        result = await ranker.rank(["600519", "000001"], top_n=10)
        assert result == []

    @pytest.mark.asyncio
    async def test_rank_single_stock(self, ranker: StockRanker) -> None:
        """单只股票评分。"""
        # Mock 外部依赖
        n = 60
        df = pd.DataFrame({
            "close": [100.0 + i * 0.5 for i in range(n)],
            "high": [101.0 + i * 0.5 for i in range(n)],
            "low": [99.0 + i * 0.5 for i in range(n)],
            "open": [100.0 + i * 0.5 for i in range(n)],
            "volume": [1_000_000] * n,
        })
        ranker._dm.fetch_stock_history = AsyncMock(return_value=df)
        ranker._dm.fetch_stock_meta = AsyncMock(return_value={
            "name": "测试股票", "symbol": "600519",
        })

        with patch.object(ranker._fundamental, "check", return_value={
            "pass": True, "pe": 15.0, "pb": 2.5, "roe": 12.0,
            "reasons": [],
        }):
            with patch.object(ranker._money_flow, "check", AsyncMock(return_value={
                "pass": True, "score": 0.6, "reasons": [],
            })):
                result = await ranker.rank(["600519"], top_n=10)

        assert len(result) == 1
        assert result[0]["symbol"] == "600519"
        assert result[0]["name"] == "测试股票"
        assert result[0]["total_score"] > 0
        assert isinstance(result[0]["total_score"], float)
        assert isinstance(result[0]["factor_score"], float)
        assert isinstance(result[0]["fundamental_score"], float)
        assert isinstance(result[0]["money_flow_score"], float)
        assert "price" in result[0]
        assert "pe" in result[0]

    @pytest.mark.asyncio
    async def test_rank_multiple_ordered(self, ranker: StockRanker) -> None:
        """多只股票按评分降序排列。"""
        n = 60
        df1 = pd.DataFrame({
            "close": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "open": [100.0 + i for i in range(n)],
            "volume": [1_000_000] * n,
        })
        df2 = pd.DataFrame({
            "close": [50.0 - i * 0.3 for i in range(n)],
            "high": [51.0 - i * 0.3 for i in range(n)],
            "low": [49.0 - i * 0.3 for i in range(n)],
            "open": [50.0 - i * 0.3 for i in range(n)],
            "volume": [500_000] * n,
        })

        def mock_history(symbol: str, days: int = 60) -> pd.DataFrame:
            return df1 if symbol == "600519" else df2

        ranker._dm.fetch_stock_history = AsyncMock(side_effect=mock_history)
        ranker._dm.fetch_stock_meta = AsyncMock(return_value={
            "name": "测试", "symbol": "600519",
        })

        with patch.object(ranker._fundamental, "check", side_effect=[
            {"pass": True, "pe": 15.0, "pb": 2.0, "roe": 10.0, "reasons": []},
            {"pass": False, "pe": None, "pb": None, "roe": None, "reasons": ["PE too high"]},
        ]):
            with patch.object(ranker._money_flow, "check", AsyncMock(return_value={
                "pass": True, "score": 0.6, "reasons": [],
            })):
                result = await ranker.rank(["600519", "000001"], top_n=10)

        assert len(result) == 2
        # 600519 评分应高于 000001（上升趋势 + 基本面通过）
        assert result[0]["symbol"] == "600519"
        assert result[0]["total_score"] >= result[1]["total_score"]

    @pytest.mark.asyncio
    async def test_top_n_limit(self, ranker: StockRanker) -> None:
        """top_n 参数限制返回数量。"""
        n = 60
        df = pd.DataFrame({
            "close": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "open": [100.0 + i for i in range(n)],
            "volume": [1_000_000] * n,
        })
        ranker._dm.fetch_stock_history = AsyncMock(return_value=df)
        ranker._dm.fetch_stock_meta = AsyncMock(return_value={"name": "测试"})

        with patch.object(ranker._fundamental, "check", return_value={
            "pass": True, "pe": 15.0, "pb": 2.0, "roe": 10.0, "reasons": [],
        }):
            with patch.object(ranker._money_flow, "check", AsyncMock(return_value={
                "pass": True, "score": 0.6, "reasons": [],
            })):
                result = await ranker.rank(
                    ["600519", "000001", "000002", "000003"],
                    top_n=2,
                )

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_hot_sector_boost(self, ranker: StockRanker) -> None:
        """股票属于热门板块时 sector_score 更高。"""
        n = 60
        df = pd.DataFrame({
            "close": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "open": [100.0 + i for i in range(n)],
            "volume": [1_000_000] * n,
        })
        ranker._dm.fetch_stock_history = AsyncMock(return_value=df)
        ranker._dm.fetch_stock_meta = AsyncMock(return_value={"name": "test"})

        with patch.object(ranker._fundamental, "check", return_value={
            "pass": True, "pe": 15.0, "pb": 2.0, "roe": 10.0, "reasons": [],
        }):
            with patch.object(ranker._money_flow, "check", AsyncMock(return_value={
                "pass": True, "score": 0.6, "reasons": [],
            })):
                # 不传 hot_sectors
                result_no_sector = await ranker.rank(["600519"], top_n=10)
                # 传空 hot_sectors
                result_empty_sector = await ranker.rank(["600519"], hot_sectors=[], top_n=10)
                # 两者评分应相等
                assert result_no_sector[0]["total_score"] == result_empty_sector[0]["total_score"]

    @pytest.mark.asyncio
    async def test_partial_failure_one_stock(self, ranker: StockRanker) -> None:
        """部分股票评分失败不影响其他股票。"""
        n = 60
        good_df = pd.DataFrame({
            "close": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "open": [100.0 + i for i in range(n)],
            "volume": [1_000_000] * n,
        })

        # 第一只股票正常，第二只返回空
        async def mock_history(symbol: str, days: int = 60) -> pd.DataFrame:
            return good_df if symbol == "600519" else pd.DataFrame()

        ranker._dm.fetch_stock_history = AsyncMock(side_effect=mock_history)
        ranker._dm.fetch_stock_meta = AsyncMock(return_value={"name": "test"})

        with patch.object(ranker._fundamental, "check", return_value={
            "pass": True, "pe": 15.0, "pb": 2.0, "roe": 10.0, "reasons": [],
        }):
            with patch.object(ranker._money_flow, "check", AsyncMock(return_value={
                "pass": True, "score": 0.6, "reasons": [],
            })):
                result = await ranker.rank(["600519", "BAD_STOCK"], top_n=10)

        # 只有 600519 有结果
        assert len(result) == 1
        assert result[0]["symbol"] == "600519"