"""Centralized stock name lookup table.

Single source of truth for local fallback stock names.
Import from here instead of duplicating in data/manager.py,
engine/main.py, and engine/signal_pipeline.py.
"""

from __future__ import annotations

from functools import lru_cache

# Mutable dict for runtime updates (e.g., from screener or manual adds).
# Keys are 6-digit string codes, values are Chinese stock names.
_STOCK_NAMES: dict[str, str] = {
    "600519": "贵州茅台",
    "300750": "宁德时代",
    "000858": "五粮液",
    "601318": "中国平安",
    "000333": "美的集团",
    "300059": "东方财富",
    "600030": "中信证券",
    "000776": "广发证券",
    "603259": "药明康德",
    "600600": "青岛啤酒",
    "002625": "光启技术",
    "600674": "川投能源",
    "688396": "华润微",
    "601238": "广汽集团",
    "600460": "士兰微",
    "000977": "浪潮信息",
    "002049": "紫光国微",
    "300033": "同花顺",
    "600026": "中远海能",
    "600150": "中国船舶",
    "600489": "中金黄金",
    "600584": "长电科技",
    "601899": "紫金矿业",
    "603019": "中科曙光",
    "603799": "华友钴业",
    "600036": "招商银行",
    "601398": "工商银行",
    "601939": "建设银行",
    "000538": "云南白药",
}


@lru_cache(maxsize=1)
def get_stock_names() -> dict[str, str]:
    """Return a frozen copy of the static fallback name table."""
    return dict(_STOCK_NAMES)


def lookup_stock_name(code: str) -> str:
    """Look up a stock name by 6-digit code. Returns empty string on miss."""
    return _STOCK_NAMES.get(code.strip().zfill(6), "")


def register_stock_name(code: str, name: str) -> None:
    """Register or update a stock name mapping at runtime."""
    _STOCK_NAMES[code.strip().zfill(6)] = name
