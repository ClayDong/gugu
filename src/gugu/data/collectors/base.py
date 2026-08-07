"""数据采集器抽象基类。

统一数据格式：
- 个股行情: DataFrame[date, open, high, low, close, volume, amount]
- 个股资金流: DataFrame[date, main_net, main_pct, super_large_net, large_net, medium_net, small_net]
- 行业资金流: DataFrame[date, sector, main_net, main_pct, super_large_net]
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Literal

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

Source = Literal["akshare", "eastmoney", "sina", "tencent"]


class BaseCollector(ABC):
    """采集器抽象基类。所有数据源采集器继承此类。"""

    source: Source = "akshare"
    adjust_type: str = "none"

    # 资金流接口熔断（2026-08-07：东财限流 RemoteDisconnected 单日刷 1705 次 ERROR）
    # 连续失败达阈值 → 熔断冷却期，冷却期内静默返回空（不调 API 不打 ERROR），到期自动恢复。
    # 放在基类：akshare/eastmoney 同源东财，限流时两边都要熔断；实例级状态各自独立。
    _FLOW_BREAKER_THRESHOLD = 3
    _FLOW_BREAKER_COOLDOWN = 1800  # 30 分钟
    _flow_fail_count = 0
    _flow_breaker_until = 0.0

    def _flow_breaker_active(self) -> bool:
        return time.time() < self._flow_breaker_until

    def _flow_record_failure(self) -> None:
        self._flow_fail_count += 1
        if self._flow_fail_count >= self._FLOW_BREAKER_THRESHOLD:
            self._flow_breaker_until = time.time() + self._FLOW_BREAKER_COOLDOWN
            self._flow_fail_count = 0
            logger.warning(
                "{} 资金流连续失败 {} 次，熔断 {} 秒（数据源限流/封禁时防刷屏，到期自动恢复）",
                self.source,
                self._FLOW_BREAKER_THRESHOLD,
                self._FLOW_BREAKER_COOLDOWN,
            )

    def _flow_record_success(self) -> None:
        self._flow_fail_count = 0
        self._flow_breaker_until = 0.0

    def _flow_breaker_guard(self, what: str) -> pd.DataFrame:
        """熔断期内直接返回空 DataFrame（静默降级，不调 API）。"""
        logger.debug(
            "{} 资金流熔断冷却中，跳过 {}（{}）", self.source, what, self._flow_breaker_until
        )
        return pd.DataFrame()

    def _set_attrs(self, df: pd.DataFrame) -> pd.DataFrame:
        df.attrs["data_source"] = self.source
        df.attrs["adjust_type"] = self.adjust_type
        df.attrs.setdefault("fetch_timestamp", pd.Timestamp.now().isoformat())
        return df

    @abstractmethod
    def fetch_stock_history(self, symbol: str, days: int = 90) -> pd.DataFrame:
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

    # ── 指数代码识别（v0.9.7 P0 修复）────────────────────────────
    # 注意：000xxx 开头的代码既有深市股票（000001 平安银行/000688 国城矿业）
    # 也有沪市指数（000300 沪深300），代码层面无法区分 → 白名单只收录
    # **确定无同名深市股票**的指数，其余指数必须由调用方显式传 sh/sz 前缀。
    # 已确认冲突（不放入白名单）：000001=平安银行, 000016=深康佳A,
    # 000688=国城矿业, 000852=石化机械, 000905=厦门港务。
    SH_INDEX_CODES: frozenset[str] = frozenset(
        {
            "000300",  # 沪深300（无同名深市股票）
        }
    )
    SZ_INDEX_CODES: frozenset[str] = frozenset(
        {
            "399001",  # 深证成指
            "399006",  # 创业板指
        }
    )
    INDEX_CODES: frozenset[str] = SH_INDEX_CODES | SZ_INDEX_CODES

    @classmethod
    def is_index_code(cls, symbol: str) -> bool:
        """判断 6 位代码是否为指数代码（沪深300/深证成指等无冲突白名单）。"""
        return cls.normalize_symbol(symbol) in cls.INDEX_CODES

    @staticmethod
    def symbol_with_prefix(symbol: str) -> str:
        """转换为带前缀格式（sh/sz/bj），支持已带前缀的代码与指数白名单。"""
        raw = symbol.strip()
        if raw[:2].lower() in ("sh", "sz", "bj") and raw[2:].isdigit():
            return raw  # 已带前缀（显式指定交易所，最高优先级）
        code = BaseCollector.normalize_symbol(symbol)
        if code in BaseCollector.SH_INDEX_CODES:
            return f"sh{code}"
        if code in BaseCollector.SZ_INDEX_CODES:
            return f"sz{code}"
        if code.startswith(("60", "68", "11", "13")):
            return f"sh{code}"
        if code.startswith(("00", "30", "12")):
            return f"sz{code}"
        if code.startswith(("43", "83", "87", "88")):
            return f"bj{code}"
        return code
