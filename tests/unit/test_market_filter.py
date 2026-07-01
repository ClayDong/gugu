"""大市过滤模块 MarketFilter 单元测试。"""
from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest

from gugu.engine.market_filter import MarketFilter


def _make_bullish_index_data(n: int = 300) -> pd.DataFrame:
    """构造牛市指数数据：价格持续上涨，成交量放大。"""
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    close = [3000 + i * 5 for i in range(n)]  # 线性上涨
    return pd.DataFrame({
        "date": dates,
        "open": close,
        "high": [c + 20 for c in close],
        "low": [c - 20 for c in close],
        "close": close,
        "volume": [100_000_000 + i * 1_000_000 for i in range(n)],  # 成交量递增
    })


def _make_bearish_index_data(n: int = 300) -> pd.DataFrame:
    """构造熊市指数数据：价格持续下跌。"""
    dates = pd.date_range("2025-01-01", periods=n, freq="D")
    close = [4000 - i * 5 for i in range(n)]  # 线性下跌
    return pd.DataFrame({
        "date": dates,
        "open": close,
        "high": [c + 20 for c in close],
        "low": [c - 20 for c in close],
        "close": close,
        "volume": [200_000_000 - i * 500_000 for i in range(n)],  # 成交量递减
    })


class TestMarketFilter:
    """MarketFilter 大市过滤测试。"""

    @pytest.mark.asyncio
    async def test_bullish_market(self):
        """牛市条件下应返回 bullish=True。"""
        mf = MarketFilter()
        mf.fetch_index_data = mock.AsyncMock(return_value=_make_bullish_index_data())
        result = await mf.check_market()
        assert result["bullish"] is True
        assert result["regime"] == "bull"
        assert result["index_close"] > result["ma200"]

    @pytest.mark.asyncio
    async def test_bearish_market(self):
        """熊市条件下应返回 bullish=False。"""
        mf = MarketFilter()
        mf.fetch_index_data = mock.AsyncMock(return_value=_make_bearish_index_data())
        result = await mf.check_market()
        assert result["bullish"] is False
        assert result["regime"] in ("bear", "range")

    @pytest.mark.asyncio
    async def test_api_failure_defaults_to_bullish(self):
        """数据获取失败时默认放行（不阻塞交易）。"""
        mf = MarketFilter()
        mf.fetch_index_data = mock.AsyncMock(side_effect=RuntimeError("network error"))
        result = await mf.check_market()
        assert result["bullish"] is True
        assert result["regime"] == "unknown"
        assert "失败" in result["reason"]

    @pytest.mark.asyncio
    async def test_cache_same_day(self):
        """同一天内多次调用只查询一次。"""
        mf = MarketFilter()
        call_count = 0

        async def mock_fetch(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_bullish_index_data()

        mf.fetch_index_data = mock_fetch
        result1 = await mf.check_market()
        result2 = await mf.check_market()
        assert call_count == 1  # 只调用了一次
        assert result1 == result2

    @pytest.mark.asyncio
    async def test_insufficient_data_defaults_to_bullish(self):
        """数据不足时默认放行。"""
        mf = MarketFilter()
        short_df = _make_bullish_index_data(50)  # 只有50天，不足200日
        mf.fetch_index_data = mock.AsyncMock(return_value=short_df)
        result = await mf.check_market()
        assert result["bullish"] is True
        assert result["regime"] in ("unknown", "bull")
