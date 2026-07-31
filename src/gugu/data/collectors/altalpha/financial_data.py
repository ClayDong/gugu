"""财务指标数据采集器。"""

from __future__ import annotations

import pandas as pd

from gugu.data.collectors.altalpha.base import AltAlphaCollector


class FinancialDataCollector(AltAlphaCollector):
    """财务指标采集器（akshare stock_financial_analysis_indicator）。"""

    _data_type = "financial_indicator"

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """校验并清洗财务指标数据。

        akshare 返回的列已经是中文，做基本清洗：
        - 过滤掉全是 NaN 的行
        """
        df = df.dropna(how="all")
        return df.reset_index(drop=True)

    async def fetch_indicators(self, symbol: str, start_year: str = "2020") -> pd.DataFrame:
        """获取个股财务指标（ROE/ROIC/营收增长等）。

        Args:
            symbol: 股票代码，如 "600519"
            start_year: 起始年份，如 "2020"

        Returns:
            DataFrame 含多项财务分析指标（中文列名）
        """
        import akshare as ak

        return await self.fetch_with_cache(
            ak.stock_financial_analysis_indicator, symbol=symbol, start_year=start_year
        )
