"""业绩预告数据采集器。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from gugu.data.collectors.altalpha.base import AltAlphaCollector


class EarningsForecastCollector(AltAlphaCollector):
    """业绩预告采集器（akshare stock_yjyg_em）。

    akshare 返回的列名：
        序号, 股票代码, 股票简称, 预测指标, 业绩变动, 预测数值,
        业绩变动幅度, 业绩变动原因, 预告类型, 上年同期值, 公告日期
    """

    _data_type = "earnings_forecast"

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """校验并标准化业绩预告数据。"""
        col_map = {
            "股票代码": "code",
            "股票简称": "name",
            "预测指标": "forecast_metric",
            "业绩变动": "change_desc",
            "预测数值": "forecast_value",
            "业绩变动幅度": "growth",  # 变动幅度（%）
            "业绩变动原因": "change_reason",
            "预告类型": "forecast_type",
            "上年同期值": "prev_value",
            "公告日期": "announce_date",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "code" in df.columns:
            df["code"] = df["code"].astype(str).str.zfill(6)
        return df.reset_index(drop=True)

    async def fetch_latest(self, year: int | None = None) -> pd.DataFrame:
        """获取指定年份业绩预告。默认当前年。

        按报告期倒序尝试：年报→三季报→中报→一季报，取首个非空结果。

        注意：不能使用 fetch_with_cache，因为不同 date 参数共享缓存键
        会导致返回错误报告期的数据。此处直接调用 API + 限流。

        Args:
            year: 报告年份，默认当前年

        Returns:
            DataFrame[code, name, forecast_metric, forecast_value, growth,
                     forecast_type, prev_value, announce_date]
        """
        import asyncio

        import akshare as ak

        if year is None:
            year = datetime.now().year

        for report_date in [f"{year}1231", f"{year}0930", f"{year}0630", f"{year}0331"]:
            try:
                await self._rate_limit()
                df = await asyncio.to_thread(ak.stock_yjyg_em, date=report_date)
                if df is not None and not df.empty:
                    df = self._validate(df)
                    # P0 修复：按报告期写入独立缓存键，避免不同年份/报告期数据互相覆盖
                    cache_key = self._build_cache_key(ak.stock_yjyg_em, date=report_date)
                    self._write_cache(df, cache_key=cache_key)
                    return df
            except Exception:
                continue
        # 降级：返回过期缓存（任意报告期均可）
        stale = self._read_cache(ignore_ttl=True)
        return stale if stale is not None else pd.DataFrame()
