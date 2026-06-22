"""降级采集器：新浪财经。

主源 akshare 连续失败 3 次后降级到此处。
使用与主源不同的 API 接口，确保真正的数据冗余。
"""
from __future__ import annotations

import akshare as ak
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from gugu.data.collectors.base import BaseCollector
from gugu.data.quality import validate_stock_history
from gugu.utils.log import get_logger

logger = get_logger()


class SinaCollector(BaseCollector):
    """新浪财经降级采集器（仅行情）。

    与主源 AkshareCollector 的差异：
    - 历史行情：使用 ak.stock_zh_a_daily（新浪源）而非 ak.stock_zh_a_hist（东方财富源）
    - 实时行情：使用 ak.stock_zh_a_spot（新浪源）而非 ak.stock_zh_a_spot_em（东方财富源）
    """

    source = "sina"

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    def fetch_stock_history(self, symbol: str, days: int = 60) -> pd.DataFrame:
        code = self.normalize_symbol(symbol)
        prefix = self.symbol_with_prefix(code)
        try:
            df = ak.stock_zh_a_daily(symbol=prefix, adjust="qfq")
        except Exception as e:
            logger.error(f"新浪历史行情 {code} 失败: {e}")
            raise

        if df.empty:
            return df
        df = df.rename(columns={
            "date": "date", "open": "open", "high": "high",
            "low": "low", "close": "close", "volume": "volume",
        })
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.tail(days).reset_index(drop=True)
        return validate_stock_history(df, code)

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    def fetch_stock_realtime(self, symbols: list[str]) -> pd.DataFrame:
        """新浪实时行情（通过 akshare 的 sina 接口，与主源东方财富接口不同）。"""
        if not symbols:
            return pd.DataFrame()

        codes = {self.normalize_symbol(s) for s in symbols}
        rows = []
        try:
            # 使用新浪源的实时行情接口，而非东方财富的 stock_zh_a_spot_em
            spot_df = ak.stock_zh_a_spot()
        except Exception as e:
            logger.error(f"新浪实时行情失败: {e}")
            return pd.DataFrame()

        if spot_df.empty:
            return pd.DataFrame()

        for code in codes:
            row = spot_df[spot_df["代码"] == code]
            if not row.empty:
                rows.append({
                    "symbol": code,
                    "name": row.iloc[0].get("名称", ""),
                    "price": row.iloc[0].get("最新价", 0),
                    "change_pct": row.iloc[0].get("涨跌幅", 0),
                    "volume": row.iloc[0].get("成交量", 0),
                    "amount": row.iloc[0].get("成交额", 0),
                })
        return pd.DataFrame(rows)

    def fetch_sector_flow(self) -> pd.DataFrame:
        """新浪无行业资金流，返回空。"""
        logger.warning("新浪源不支持行业资金流，返回空")
        return pd.DataFrame()

    def fetch_stock_flow(self, symbol: str) -> pd.DataFrame:
        """新浪无个股资金流明细，返回空。"""
        logger.warning("新浪源不支持个股资金流明细，返回空")
        return pd.DataFrame()
