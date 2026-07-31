"""限售解禁数据采集器。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from gugu.data.collectors.altalpha.base import AltAlphaCollector


class ShareUnlockCollector(AltAlphaCollector):
    """限售解禁采集器（akshare stock_restricted_release_detail_em）。

    批量获取指定日期范围内的全部个股解禁明细。
    """

    _data_type = "share_unlock"

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """校验并标准化限售解禁数据。"""
        col_map = {
            "股票代码": "code",
            "股票简称": "name",
            "解禁时间": "unlock_date",
            "解禁数量": "unlock_shares",
            "实际解禁数量": "actual_unlock",
            "实际解禁市值": "unlock_value",
            "限售股类型": "restricted_type",
            "占解禁前流通市值比例": "float_ratio",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.zfill(6)
        return df.reset_index(drop=True)

    async def fetch_upcoming(self, days: int = 30) -> pd.DataFrame:
        """获取未来N日全市场解禁明细。

        使用 stock_restricted_release_detail_em 批量接口，一次返回所有股票。

        Args:
            days: 未来N日窗口

        Returns:
            DataFrame[code, name, unlock_date, unlock_shares, actual_unlock,
                     unlock_value, restricted_type, float_ratio]
        """
        import akshare as ak

        now = datetime.now()
        end = (now + timedelta(days=days)).strftime("%Y%m%d")
        start = now.strftime("%Y%m%d")

        try:
            df = await self.fetch_with_cache(
                ak.stock_restricted_release_detail_em,
                start_date=start,
                end_date=end,
            )
            if df is not None and not df.empty:
                df = self._validate(df)
            return df
        except Exception:
            return pd.DataFrame()

    async def fetch_for_symbol(self, symbol: str) -> pd.DataFrame:
        """获取单只股票的限售解禁批次（按个股查询，用于精确分析）。

        Args:
            symbol: 股票代码（6位）

        Returns:
            DataFrame[unlock_date, unlock_shares, actual_unlock,
                     unlock_value, restricted_type, float_ratio]
        """
        import akshare as ak

        try:
            df = await self.fetch_with_cache(ak.stock_restricted_release_queue_em, symbol=symbol)
            if df is not None and not df.empty:
                df = self._validate(df)
                df["symbol"] = symbol
            return df
        except Exception:
            return pd.DataFrame()
