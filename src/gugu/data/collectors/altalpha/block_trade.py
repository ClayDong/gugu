"""大宗交易数据采集器。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from gugu.data.collectors.altalpha.base import AltAlphaCollector


class BlockTradeCollector(AltAlphaCollector):
    """大宗交易采集器（akshare stock_dzjy_mrmx）。

    akshare 返回的列名：
        序号, 交易日期, 证券代码, 证券简称, 成交价, 成交量, 成交额,
        买方营业部, 卖方营业部

    注意：akshare 不直接返回收盘价和折溢率，需另行获取收盘价计算折价率。
    """

    _data_type = "block_trade"

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """校验并标准化大宗交易数据。"""
        col_map = {
            "证券代码": "code",
            "证券简称": "name",
            "交易日期": "trade_date",
            "成交价": "deal_price",
            "成交量": "volume",
            "成交额": "amount",
            "买方营业部": "buyer",
            "卖方营业部": "seller",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.zfill(6)
        return df.reset_index(drop=True)

    async def fetch_detail(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        """获取大宗交易明细。默认近30日。

        Args:
            start_date: YYYYMMDD，默认近35日
            end_date: YYYYMMDD，默认今日

        Returns:
            DataFrame[code, name, trade_date, deal_price, volume, amount, buyer, seller]
        """
        import akshare as ak

        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=35)).strftime("%Y%m%d")
        return await self.fetch_with_cache(
            ak.stock_dzjy_mrmx, start_date=start_date, end_date=end_date
        )
