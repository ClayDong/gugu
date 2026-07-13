"""个股综合评分：集成基本面、资金流、因子、板块热度，对股票池排名。

参考 qlib 的 TopkDropoutStrategy：在候选池中选TopK，然后逐个验证策略信号。
"""

from __future__ import annotations

import asyncio
from typing import Any

from gugu.analysis.alpha_factory import AlphaFactory
from gugu.data import data_manager
from gugu.filters.fundamental import FundamentalFilter
from gugu.filters.money_flow import MoneyFlowFilter
from gugu.utils.log import get_logger

logger = get_logger()


class StockRanker:
    """个股综合评分器。

    评分维度（v0.6.0 调整权重）：
    1. 因子评分（25%）：26个Alpha因子的综合评分
    2. 基本面评分（20%）：PE/PB/ROE/营收增长
    3. 资金流评分（25%）：主力净流入/5日净流入
    4. 板块强度（15%）：是否属于当前强势板块
    5. 筹码画像（15%）：户数变化+户均持股+机构持仓+流通市值

    输出：排序后的股票列表 + 评分详情
    """

    def __init__(self):
        self._dm = data_manager()
        self._alpha = AlphaFactory()
        self._fundamental = FundamentalFilter()
        self._money_flow = MoneyFlowFilter()
        # v0.6.0: 筹码画像评分（复用现有 HolderConcentrationFilter）
        from gugu.filters.holder_concentration import HolderConcentrationFilter

        self._holder_concentration = HolderConcentrationFilter()

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
        if not symbols:
            return []

        hot_sectors = hot_sectors or []

        # 并发评分：原实现为串行 for 循环，N 只股票需 N 次串行 I/O
        # 优化为 asyncio.gather 并发评分，大幅缩短评分时间
        sem = asyncio.Semaphore(10)  # 限制并发数

        async def _score_with_sem(symbol: str) -> dict[str, Any] | None:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._score_one(symbol, hot_sectors), timeout=10.0
                    )
                except TimeoutError:
                    logger.debug(f"评分超时 {symbol}")
                except Exception as e:
                    logger.debug(f"评分失败 {symbol}: {e}")
                return None

        tasks = [_score_with_sem(sym) for sym in symbols]
        results_list = await asyncio.gather(*tasks)
        results = [r for r in results_list if r is not None]

        # 按总分排序
        results.sort(key=lambda x: x["total_score"], reverse=True)
        return results[:top_n]

    async def _score_one(self, symbol: str, hot_sectors: list[str]) -> dict[str, Any] | None:
        """对单只股票评分"""
        # 获取K线数据
        df = await self._dm.fetch_stock_history(symbol, days=90)
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

        # 5. 筹码画像评分（15%权重）v0.6.0 新增
        hc_result = await self._holder_concentration.check(symbol)
        # 获取流通市值（从实时快照，降级为0）
        circ_mv = 0.0
        try:
            rt = await self._dm.fetch_stock_realtime([symbol])
            if not rt.empty:
                # circ_mv 字段单位为元，转为亿元
                circ_mv = float(rt.iloc[0].get("circ_mv", 0) or 0) / 1e8
        except Exception:
            pass
        chip_score = self._compute_chip_score(hc_result, circ_mv=circ_mv)

        # 加权总分 v0.6.0: 新增筹码画像15%，调整其他维度权重
        total = (
            factor_score * 0.25
            + fund_score * 0.20
            + flow_score * 0.25
            + sector_score * 0.15
            + chip_score * 0.15
        )

        return {
            "symbol": symbol,
            "name": name,
            "total_score": round(total, 4),
            "factor_score": round(factor_score, 4),
            "fundamental_score": round(fund_score, 4),
            "money_flow_score": round(flow_score, 4),
            "sector_score": round(sector_score, 4),
            "chip_score": round(chip_score, 4),  # v0.6.0 新增
            "industry": industry,
            "price": round(float(df.iloc[-1]["close"]), 2),
            "pe": fund.get("pe"),
            "pb": fund.get("pb"),
            "roe": fund.get("roe"),
        }

    def _compute_chip_score(self, hc_result: dict, circ_mv: float = 0.0) -> float:
        """计算筹码画像评分（0-1）。

        评分维度（满分1.0）：
        - 户数变化（0.4）：连续下降>20% → 0.4；单季下降>5% → 0.3；
          稳定±5% → 0.2；单季上升>5% → 0.1；连续上升>20% → 0.0
        - 户均持股变化（0.3）：上升>15% → 0.3；上升5-15% → 0.2；
          稳定±5% → 0.15；下降>5% → 0.05；下降>20% → 0.0
        - 机构持仓（0.2）：>40% → 0.2；20-40% → 0.15；10-20% → 0.1；
          <10% → 0.05；数据缺失 → 0.1（中性）
        - 流通市值（0.1）：50-500亿 → 0.1；500-2000亿 → 0.05；其他 → 0.03

        特殊处理：
        - 避雷针 → 0.0（主力派发，直接拒绝）
        - 数据缺失 → 0.5（中性默认分，不惩罚也不奖励）

        Args:
            hc_result: HolderConcentrationFilter.check() 返回的字典
            circ_mv: 流通市值（亿元），0 表示数据不可用

        Returns:
            筹码画像评分（0.0-1.0）
        """
        # 避雷针 → 0.0
        if hc_result.get("label") == "避雷针":
            return 0.0

        # 数据缺失 → 0.5（中性）
        if hc_result.get("data_missing"):
            return 0.5

        holder_num_pct = float(hc_result.get("holder_num_pct", 0.0) or 0.0)
        avg_hold_change = float(hc_result.get("avg_hold_change", 0.0) or 0.0)
        org_hold_pct = float(hc_result.get("org_hold_pct", 0.0) or 0.0)

        # 1. 户数变化评分（0.4满分）
        if holder_num_pct < -20:
            score_holder = 0.4  # 连续下降>20%
        elif holder_num_pct < -5:
            score_holder = 0.3  # 单季下降>5%
        elif abs(holder_num_pct) <= 5:
            score_holder = 0.2  # 稳定
        elif holder_num_pct < 20:
            score_holder = 0.1  # 单季上升>5%
        else:
            score_holder = 0.0  # 连续上升>20%

        # 2. 户均持股变化评分（0.3满分）
        if avg_hold_change > 15:
            score_avg = 0.3
        elif avg_hold_change > 5:
            score_avg = 0.2
        elif abs(avg_hold_change) <= 5:
            score_avg = 0.15
        elif avg_hold_change > -20:
            score_avg = 0.05
        else:
            score_avg = 0.0

        # 3. 机构持仓评分（0.2满分）— 数据缺失时中性0.1
        if org_hold_pct > 40:
            score_org = 0.2
        elif org_hold_pct > 20:
            score_org = 0.15
        elif org_hold_pct > 10:
            score_org = 0.1
        elif org_hold_pct > 0:
            score_org = 0.05
        else:
            score_org = 0.1  # 数据缺失（org_hold_pct=0），中性

        # 4. 流通市值评分（0.1满分）
        if 50 <= circ_mv <= 500:
            score_mv = 0.1
        elif 500 < circ_mv <= 2000:
            score_mv = 0.05
        else:
            score_mv = 0.03

        return round(score_holder + score_avg + score_org + score_mv, 4)
