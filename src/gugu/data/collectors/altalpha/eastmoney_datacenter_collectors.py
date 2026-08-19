"""基于东财数据中心的另类数据采集器。

这些 Collector 保持 DataFrame 输出，与现有 AltAlphaFactorEngine 兼容。
akshare 版本仍保留，由调用方决定是否作为旧路径 fallback。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from gugu.data.collectors.altalpha.base import AltAlphaCollector
from gugu.data.evidence import set_evidence
from gugu.data.providers import EastmoneyDataCenter, eastmoney_datacenter


def _records_to_df(
    records: list[dict[str, Any]],
    *,
    data_type: str,
    mapper: Callable[[dict[str, Any]], dict[str, Any]],
    unit: str = "",
    period: str = "daily_or_event",
) -> pd.DataFrame:
    rows = [mapper(record) for record in records]
    df = pd.DataFrame(rows)
    return set_evidence(
        df,
        data_source="eastmoney_datacenter",
        period=period,
        unit=unit,
        quality_status="validated" if not df.empty else "empty",
        data_available=not df.empty,
    )


class EastmoneyDragonTigerCollector(AltAlphaCollector):
    """东财数据中心龙虎榜明细。"""

    _data_type = "dragon_tiger_datacenter"
    _data_source = "eastmoney_datacenter"
    _period = "daily_or_event"
    _unit = "万元"

    def __init__(
        self,
        provider: EastmoneyDataCenter | None = None,
        cache_dir: Path | str | None = None,
    ) -> None:
        super().__init__(cache_dir=cache_dir)
        self._provider = provider or eastmoney_datacenter()

    @staticmethod
    def _map(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": str(row.get("SECURITY_CODE", "")).zfill(6),
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "date": str(row.get("TRADE_DATE", ""))[:10],
            "close": row.get("CLOSE_PRICE", 0),
            "change_pct": row.get("CHANGE_RATE", 0),
            "lhb_net_buy": (row.get("BILLBOARD_NET_AMT") or 0) / 10000,
            "lhb_buy": (row.get("BILLBOARD_BUY_AMT") or 0) / 10000,
            "lhb_sell": (row.get("BILLBOARD_SELL_AMT") or 0) / 10000,
            "turnover_rate": row.get("TURNOVERRATE", 0),
            "reason": row.get("EXPLANATION", ""),
        }

    async def fetch_detail(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        end_date = end_date or datetime.now().strftime("%Y-%m-%d")
        start_date = start_date or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        cache_key = self._build_cache_key(self.fetch_detail, start_date, end_date)
        cached = self._read_cache(cache_key=cache_key)
        if cached is not None:
            return self._cache_hit(cached)
        try:
            records = self._provider.query(
                "RPT_DAILYBILLBOARD_DETAILSNEW",
                filter_str=f"(TRADE_DATE>='{start_date}')(TRADE_DATE<='{end_date}')",
                page_size=500,
                sort_columns="TRADE_DATE",
                sort_types="-1",
            )
        except Exception:
            return self._stale_or_unavailable(cache_key)
        df = _records_to_df(
            records,
            data_type=self._data_type,
            mapper=self._map,
            unit="万元",
        )
        if not df.empty:
            self._write_cache(df, cache_key=cache_key)
        return df


class EastmoneyShareUnlockCollector(AltAlphaCollector):
    """东财数据中心限售解禁明细。"""

    _data_type = "share_unlock_datacenter"
    _data_source = "eastmoney_datacenter"
    _period = "event"
    _unit = "股/百分比"

    def __init__(
        self,
        provider: EastmoneyDataCenter | None = None,
        cache_dir: Path | str | None = None,
    ) -> None:
        super().__init__(cache_dir=cache_dir)
        self._provider = provider or eastmoney_datacenter()

    @staticmethod
    def _map(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": str(row.get("SECURITY_CODE", "")).zfill(6),
            "name": row.get("SECURITY_NAME_ABBR", ""),
            "unlock_date": str(row.get("FREE_DATE", ""))[:10],
            "unlock_shares": row.get("FREE_SHARES_NUM", 0),
            "float_ratio": row.get("FREE_RATIO", 0),
            "restricted_type": row.get("LIMITED_STOCK_TYPE", ""),
        }

    async def fetch_upcoming(self, days: int = 30) -> pd.DataFrame:
        if days <= 0:
            raise ValueError("days 必须为正数")
        start = datetime.now().strftime("%Y-%m-%d")
        end = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        cache_key = self._build_cache_key(self.fetch_upcoming, days)
        cached = self._read_cache(cache_key=cache_key)
        if cached is not None:
            return self._cache_hit(cached)
        try:
            records = self._provider.query(
                "RPT_LIFT_STAGE",
                filter_str=f"(FREE_DATE>='{start}')(FREE_DATE<='{end}')",
                page_size=500,
                sort_columns="FREE_DATE",
                sort_types="1",
            )
        except Exception:
            return self._stale_or_unavailable(cache_key)
        df = _records_to_df(
            records,
            data_type=self._data_type,
            mapper=self._map,
            unit="股/百分比",
            period=f"forward_{days}d",
        )
        if not df.empty:
            self._write_cache(df, cache_key=cache_key)
        return df


class EastmoneyMarginTradeCollector(AltAlphaCollector):
    """东财数据中心融资融券明细。"""

    _data_type = "margin_trade_datacenter"
    _data_source = "eastmoney_datacenter"
    _period = "daily"
    _unit = "元"

    def __init__(
        self,
        provider: EastmoneyDataCenter | None = None,
        cache_dir: Path | str | None = None,
    ) -> None:
        super().__init__(cache_dir=cache_dir)
        self._provider = provider or eastmoney_datacenter()

    @staticmethod
    def _map(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": str(row.get("SCODE", row.get("SECURITY_CODE", ""))).zfill(6),
            "trade_date": str(row.get("DATE", ""))[:10],
            "margin_balance": row.get("RZYE", 0),
            "margin_buy": row.get("RZMRE", 0),
            "margin_repay": row.get("RZCHE", 0),
            "short_balance": row.get("RQYE", 0),
            "short_sell": row.get("RQMCL", 0),
            "short_repay": row.get("RQCHL", 0),
            "total_balance": row.get("RZRQYE", 0),
        }

    async def fetch_detail(self, symbol: str, page_size: int = 30) -> pd.DataFrame:
        code = str(symbol).strip().replace(".", "")[-6:].zfill(6)
        cache_key = self._build_cache_key(self.fetch_detail, code, page_size)
        cached = self._read_cache(cache_key=cache_key)
        if cached is not None:
            return self._cache_hit(cached)
        try:
            records = self._provider.query(
                "RPTA_WEB_RZRQ_GGMX",
                filter_str=f'(SCODE="{code}")',
                page_size=page_size,
                sort_columns="DATE",
                sort_types="-1",
            )
        except Exception:
            return self._stale_or_unavailable(cache_key)
        df = _records_to_df(
            records,
            data_type=self._data_type,
            mapper=self._map,
            unit="元",
            period="daily",
        )
        if not df.empty:
            self._write_cache(df, cache_key=cache_key)
        return df


class EastmoneyBlockTradeCollector(AltAlphaCollector):
    """东财数据中心大宗交易明细。"""

    _data_type = "block_trade_datacenter"
    _data_source = "eastmoney_datacenter"
    _period = "event"
    _unit = "元/股"

    def __init__(
        self,
        provider: EastmoneyDataCenter | None = None,
        cache_dir: Path | str | None = None,
    ) -> None:
        super().__init__(cache_dir=cache_dir)
        self._provider = provider or eastmoney_datacenter()

    @staticmethod
    def _map(row: dict[str, Any]) -> dict[str, Any]:
        close = float(row.get("CLOSE_PRICE") or 0)
        deal_price = float(row.get("DEAL_PRICE") or 0)
        premium = round((deal_price / close - 1) * 100, 2) if close else None
        return {
            "code": str(row.get("SECURITY_CODE", "")).zfill(6),
            "trade_date": str(row.get("TRADE_DATE", ""))[:10],
            "deal_price": deal_price,
            "close": close,
            "premium_pct": premium,
            "volume": row.get("DEAL_VOLUME", 0),
            "amount": row.get("DEAL_AMT", 0),
            "buyer": row.get("BUYER_NAME", ""),
            "seller": row.get("SELLER_NAME", ""),
        }

    async def fetch_detail(self, symbol: str, page_size: int = 20) -> pd.DataFrame:
        code = str(symbol).strip().replace(".", "")[-6:].zfill(6)
        cache_key = self._build_cache_key(self.fetch_detail, code, page_size)
        cached = self._read_cache(cache_key=cache_key)
        if cached is not None:
            return self._cache_hit(cached)
        try:
            records = self._provider.query(
                "RPT_DATA_BLOCKTRADE",
                filter_str=f'(SECURITY_CODE="{code}")',
                page_size=page_size,
                sort_columns="TRADE_DATE",
                sort_types="-1",
            )
        except Exception:
            return self._stale_or_unavailable(cache_key)
        df = _records_to_df(
            records,
            data_type=self._data_type,
            mapper=self._map,
            unit="元/股",
            period="event",
        )
        if not df.empty:
            self._write_cache(df, cache_key=cache_key)
        return df
