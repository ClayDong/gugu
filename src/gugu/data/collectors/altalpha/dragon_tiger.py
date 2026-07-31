"""龙虎榜数据采集器。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from gugu.data.collectors.altalpha.base import AltAlphaCollector


class DragonTigerCollector(AltAlphaCollector):
    """龙虎榜采集器（akshare stock_lhb_detail_em）。

    akshare 返回的列名：
        序号, 代码, 名称, 上榜日, 解读, 收盘价, 涨跌幅,
        龙虎榜净买额, 龙虎榜买入额, 龙虎榜卖出额, 龙虎榜成交额,
        市场总成交额, 净买额占总成交比, 成交额占总成交比,
        换手率, 流通市值, 上榜原因, 上榜后1日/2日/5日/10日
    """

    _data_type = "dragon_tiger"

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """校验并标准化龙虎榜数据。"""
        col_map = {
            "代码": "code",
            "名称": "name",
            "上榜日": "date",
            "收盘价": "close",
            "涨跌幅": "change_pct",
            "龙虎榜净买额": "lhb_net_buy",
            "龙虎榜买入额": "lhb_buy",
            "龙虎榜卖出额": "lhb_sell",
            "龙虎榜成交额": "lhb_amount",
            "净买额占总成交比": "lhb_net_ratio",
            "成交额占总成交比": "lhb_amount_ratio",
            "换手率": "turnover_rate",
            "流通市值": "float_market_cap",
            "上榜原因": "reason",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.zfill(6)
        return df.reset_index(drop=True)

    async def fetch_detail(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> pd.DataFrame:
        """获取龙虎榜明细。

        Args:
            start_date: YYYYMMDD，默认近7日
            end_date: YYYYMMDD，默认今日

        Returns:
            DataFrame[code, name, date, close, change_pct,
                     lhb_net_buy, lhb_buy, lhb_sell, lhb_amount,
                     lhb_net_ratio, lhb_amount_ratio, turnover_rate,
                     float_market_cap, reason]
        """
        import akshare as ak

        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
        return await self.fetch_with_cache(
            ak.stock_lhb_detail_em, start_date=start_date, end_date=end_date
        )
