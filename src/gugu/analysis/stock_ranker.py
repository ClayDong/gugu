"""个股综合评分：集成基本面、资金流、因子、板块热度，对股票池排名。

参考 qlib 的 TopkDropoutStrategy：在候选池中选TopK，然后逐个验证策略信号。
"""
from __future__ import annotations

import asyncio
from typing import Any

from gugu.analysis.alpha_factory import AlphaFactory
from gugu.filters.fundamental import FundamentalFilter
from gugu.filters.money_flow import MoneyFlowFilter
from gugu.data import data_manager
from gugu.utils.log import get_logger

logger = get_logger()


class StockRanker:
    """个股综合评分器。

    评分维度：
    1. 因子评分（30%）：26个Alpha因子的综合评分
    2. 基本面评分（25%）：PE/PB/ROE/营收增长
    3. 资金流评分（25%）：主力净流入/5日净流入
    4. 板块强度（20%）：是否属于当前强势板块

    输出：排序后的股票列表 + 评分详情
    """

    def __init__(self):
        self._dm = data_manager()
        self._alpha = AlphaFactory()
        self._fundamental = FundamentalFilter()
        self._money_flow = MoneyFlowFilter()

    async def rank(
        self,
        symbols: list[str],
        hot_sectors: list[str] | None = None,
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """对股票池进行综合评分排名。

        Args:
            symbols: 股票代码列表
            hot_sectors: 当前强势板块（可选）
            top_n: 返回前N只

        Returns:
            list[dict]: 排名结果，每项包含 symbol/name/total_score/维度得分
        """
        results = []

        for symbol in symbols:
            try:
                score_detail = await self._score_one(symbol, hot_sectors or [])
                if score_detail:
                    results.append(score_detail)
            except Exception as e:
                logger.debug(f"评分失败 {symbol}: {e}")

        # 按总分排序
        results.sort(key=lambda x: x["total_score"], reverse=True)
        return results[:top_n]

    async def _score_one(self, symbol: str, hot_sectors: list[str]) -> dict[str, Any] | None:
        """对单只股票评分"""
        # 获取K线数据
        df = await self._dm.fetch_stock_history(symbol, days=60)
        if df.empty:
            return None

        # 获取元数据
        meta = await self._dm.fetch_stock_meta(symbol)
        name = meta.get("name", symbol)

        # 获取行业（用于板块评分）
        from gugu.filters.industry_constraint import IndustryConstraint
        industry_checker = IndustryConstraint()
        industry = industry_checker.get_industry(symbol)

        # 1. 因子评分（30%权重）
        factors = self._alpha.compute_all(df)
        factor_result = self._alpha.composite_score(factors)
        factor_score = max(0, factor_result["score"])  # 负分归零

        # 2. 基本面评分（25%权重）
        fund = self._fundamental.check(symbol)
        fund_score = 0.0
        if fund["pass"]:
            fund_score = 0.5
            # 加分项
            pe = fund.get("pe", 0) or 0
            pb = fund.get("pb", 0) or 0
            roe = fund.get("roe", 0) or 0
            if 0 < pe < 20:
                fund_score += 0.25
            if 0 < pb < 3:
                fund_score += 0.15
            if roe > 10:
                fund_score += 0.10

        # 3. 资金流评分（25%权重）
        flow = await self._money_flow.check(symbol)
        flow_score = flow.get("score", 0) or 0
        if flow["pass"]:
            flow_score = max(flow_score, 0.4)

        # 4. 板块评分（20%权重）
        sector_score = 0.0
        if industry and industry in hot_sectors:
            sector_score = 1.0
        elif industry:
            from gugu.analysis.sector_rotation import SW_INDUSTRY_MAP
            cat = SW_INDUSTRY_MAP.get(industry, "")
            hot_cats = {SW_INDUSTRY_MAP.get(s, "") for s in hot_sectors}
            if cat in hot_cats:
                sector_score = 0.5

        # 加权总分
        total = (
            factor_score * 0.30 +
            fund_score * 0.25 +
            flow_score * 0.25 +
            sector_score * 0.20
        )

        return {
            "symbol": symbol,
            "name": name,
            "total_score": round(total, 4),
            "factor_score": round(factor_score, 4),
            "fundamental_score": round(fund_score, 4),
            "money_flow_score": round(flow_score, 4),
            "sector_score": round(sector_score, 4),
            "industry": industry,
            "price": round(float(df.iloc[-1]["close"]), 2),
            "pe": fund.get("pe"),
            "pb": fund.get("pb"),
            "roe": fund.get("roe"),
        }