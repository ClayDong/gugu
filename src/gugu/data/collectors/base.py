"""数据采集器抽象基类。

统一数据格式：
- 个股行情: DataFrame[date, open, high, low, close, volume, amount]
- 个股资金流: DataFrame[date, main_net, main_pct, super_large_net, large_net, medium_net, small_net]
- 行业资金流: DataFrame[date, sector, main_net, main_pct, super_large_net]
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

import pandas as pd

Source = Literal["akshare", "eastmoney", "sina", "tencent"]


class BaseCollector(ABC):
    """采集器抽象基类。所有数据源采集器继承此类。"""

    source: Source = "akshare"

    @abstractmethod
    def fetch_stock_history(
        self, symbol: str, days: int = 60
    ) -> pd.DataFrame:
        """获取个股历史行情。

        Args:
            symbol: 股票代码，如 "600519"
            days: 获取近 N 个交易日

        Returns:
            DataFrame[date, open, high, low, close, volume, amount]
        """
        ...

    @abstractmethod
    def fetch_stock_realtime(self, symbols: list[str]) -> pd.DataFrame:
        """获取个股实时行情。

        Returns:
            DataFrame[symbol, name, price, change_pct, volume, amount]
        """
        ...

    @abstractmethod
    def fetch_sector_flow(self) -> pd.DataFrame:
        """获取行业资金流排名。

        Returns:
            DataFrame[sector, main_net, main_pct, super_large_net, large_net, change_pct]
        """
        ...

    @abstractmethod
    def fetch_stock_flow(self, symbol: str) -> pd.DataFrame:
        """获取个股资金流明细。

        Returns:
            DataFrame[date, main_net, main_pct, super_large_net, large_net, medium_net, small_net]
        """
        ...

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        """归一化股票代码为 6 位数字。"""
        return symbol.strip().zfill(6)

    @staticmethod
    def symbol_with_prefix(symbol: str) -> str:
        """转换为 akshare 带前缀格式（sh/sz/bj）。"""
        code = BaseCollector.normalize_symbol(symbol)
        if code.startswith(("60", "68", "11", "13")):
            return f"sh{code}"
        if code.startswith(("00", "30", "12")):
            return f"sz{code}"
        if code.startswith(("43", "83", "87", "88")):
            return f"bj{code}"
        return code
