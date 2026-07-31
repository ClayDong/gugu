"""融资融券数据采集器。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from gugu.data.collectors.altalpha.base import AltAlphaCollector


class MarginTradeCollector(AltAlphaCollector):
    """融资融券采集器（akshare stock_margin_detail_sse / szse）。

    沪市 stock_margin_detail_sse 返回列名：
        信用交易日期, 标的证券代码, 标的证券简称, 融资余额, 融资买入额,
        融资偿还额, 融券余量, 融券卖出量, 融券偿还量

    深市 stock_margin_detail_szse 返回列名：
        证券代码, 证券简称, 融资买入额, 融资余额, 融券卖出量, 融券余量,
        融券余额, 融资融券余额
    """

    _data_type = "margin_trade"

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """校验并标准化融资融券数据。"""
        col_map = {
            "标的证券代码": "code",
            "证券代码": "code",
            "标的证券简称": "name",
            "证券简称": "name",
            "信用交易日期": "trade_date",
            "融资余额": "margin_balance",
            "融资买入额": "margin_buy",
            "融资偿还额": "margin_repay",
            "融券余量": "short_balance",
            "融券卖出量": "short_sell",
            "融券偿还量": "short_repay",
            "融券余额": "short_amount",
            "融资融券余额": "total_balance",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.zfill(6)
        return df.reset_index(drop=True)

    async def fetch_detail(self, date_str: str | None = None) -> pd.DataFrame:
        """获取指定日期融资融券明细。默认昨日（T+1数据）。

        分别获取沪市和深市，合并返回。

        注意：不使用 fetch_with_cache 分别获取沪/深两市，因为两者共享同一
        _data_type 缓存键，会导致第二次调用命中第一次的缓存而跳过 API。
        此处改为手动限流+采集，合并后统一校验与缓存。

        Args:
            date_str: YYYYMMDD，默认昨日

        Returns:
            DataFrame[code, name, margin_balance, margin_buy, margin_repay,
                     short_balance, short_sell, short_repay, total_balance, market]
        """
        import asyncio

        import akshare as ak

        if date_str is None:
            # T+1数据，取昨日
            date_str = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

        # P0 修复：使用含 date_str 的缓存键，避免不同日期/市场数据互相覆盖
        cache_key = self._build_cache_key(self.fetch_detail, date_str=date_str)
        cached = self._read_cache(cache_key=cache_key)
        if cached is not None:
            return cached

        # 分别获取沪市和深市，合并
        results: list[pd.DataFrame] = []
        try:
            await self._rate_limit()
            df_sh = await asyncio.to_thread(ak.stock_margin_detail_sse, date=date_str)
            if df_sh is not None and not df_sh.empty:
                df_sh = df_sh.rename(
                    columns={
                        "标的证券代码": "code",
                        "标的证券简称": "name",
                        "信用交易日期": "trade_date",
                        "融资余额": "margin_balance",
                        "融资买入额": "margin_buy",
                        "融资偿还额": "margin_repay",
                        "融券余量": "short_balance",
                        "融券卖出量": "short_sell",
                        "融券偿还量": "short_repay",
                    }
                )
                df_sh["market"] = "sh"
                results.append(df_sh)
        except Exception:
            pass

        try:
            await self._rate_limit()
            df_sz = await asyncio.to_thread(ak.stock_margin_detail_szse, date=date_str)
            if df_sz is not None and not df_sz.empty:
                df_sz = df_sz.rename(
                    columns={
                        "证券代码": "code",
                        "证券简称": "name",
                        "融资买入额": "margin_buy",
                        "融资余额": "margin_balance",
                        "融券卖出量": "short_sell",
                        "融券余量": "short_balance",
                        "融券余额": "short_amount",
                        "融资融券余额": "total_balance",
                    }
                )
                df_sz["market"] = "sz"
                results.append(df_sz)
        except Exception:
            pass

        if results:
            merged = pd.concat(results, ignore_index=True)
            if "code" in merged.columns:
                merged["code"] = merged["code"].astype(str).str.zfill(6)
            self._write_cache(merged, cache_key=cache_key)
            return merged

        # 降级：返回过期缓存
        stale = self._read_cache(ignore_ttl=True, cache_key=cache_key)
        if stale is not None:
            return stale
        return pd.DataFrame()
