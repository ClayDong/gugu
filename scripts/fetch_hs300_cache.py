"""拉取沪深300指数日K线并缓存为 parquet（供回测集成 MarketFilter 使用）。

用法:
    uv run python scripts/fetch_hs300_cache.py

内部使用 MarketFilter.fetch_index_data 的三级冗余获取机制。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from gugu.engine.market_filter import MarketFilter
from gugu.utils.log import get_logger

logger = get_logger()
_CACHE_DIR = Path("data/parquet_cache")
_CACHE_FILE = _CACHE_DIR / "hs300_500d.parquet"


async def main() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    filter_ = MarketFilter()
    df = await filter_.fetch_index_data(days=500)
    if df.empty:
        logger.error("HS300 数据获取失败，无法缓存")
        return
    df.to_parquet(_CACHE_FILE, index=False)
    logger.info(f"HS300 数据已缓存: {_CACHE_FILE} ({len(df)} 行)")
    print(f"HS300 数据: {df['date'].iloc[0]} → {df['date'].iloc[-1]}, {len(df)} 行")


if __name__ == "__main__":
    asyncio.run(main())