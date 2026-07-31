"""高管增减持数据采集器。"""

from __future__ import annotations

import pandas as pd

from gugu.data.collectors.altalpha.base import AltAlphaCollector


class InsiderTradingCollector(AltAlphaCollector):
    """高管增减持采集器（akshare stock_ggcg_em）。

    akshare 返回的列名：
        代码, 名称, 最新价, 涨跌幅, 股东名称,
        持股变动信息-增减, 持股变动信息-变动数量,
        持股变动信息-占总股本比例, 持股变动信息-占流通股比例,
        变动后持股情况-持股总数, 变动后持股情况-占总股本比例,
        变动后持股情况-持流通股数, 变动后持股情况-占流通股比例,
        变动开始日, 变动截止日, 公告日
    """

    _data_type = "insider_trading"

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """校验并标准化高管增减持数据。"""
        col_map = {
            "代码": "code",
            "名称": "name",
            "最新价": "avg_price",  # 用最新价作为成交价近似
            "股东名称": "changer",
            "持股变动信息-增减": "direction",  # 增持/减持
            "持股变动信息-变动数量": "change_shares",
            "持股变动信息-占总股本比例": "change_ratio_total",
            "持股变动信息-占流通股比例": "change_ratio_float",
            "变动后持股情况-持股总数": "after_hold_total",
            "变动开始日": "change_start_date",
            "变动截止日": "change_end_date",
            "公告日": "announcement_date",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        # 过滤无效数据
        if "change_shares" in df.columns:
            df["change_shares"] = pd.to_numeric(df["change_shares"], errors="coerce")
            df = df[df["change_shares"].abs() > 0]
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.zfill(6)
        # 标准化日期字段
        for date_col in ["change_start_date", "change_end_date", "announcement_date"]:
            if date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        # 补充 change_date（用于因子计算的日期过滤）
        if "announcement_date" in df.columns:
            df["change_date"] = df["announcement_date"]
        return df.reset_index(drop=True)

    async def fetch_recent(self, days: int = 30) -> pd.DataFrame:
        """获取近N日全市场高管增减持明细。

        Args:
            days: 近N日（akshare stock_ggcg_em 返回全部数据，此处按公告日过滤）

        Returns:
            DataFrame[code, name, avg_price, changer, direction, change_shares,
                     change_ratio_total, change_ratio_float, after_hold_total,
                     change_start_date, change_end_date, announcement_date, change_date]
        """
        import akshare as ak

        df = await self.fetch_with_cache(ak.stock_ggcg_em, symbol="全部")
        if df is not None and not df.empty:
            df = self._validate(df)
            # 按公告日过滤近N日
            if "change_date" in df.columns and not df.empty:
                cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
                df = df[df["change_date"] >= cutoff].reset_index(drop=True)
        return df
