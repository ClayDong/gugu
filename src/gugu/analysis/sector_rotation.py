"""板块轮动检测：找出当前强势板块，指导选股方向。

A股板块轮动规律：
1. 资金流向是核心驱动力
2. 涨跌幅排名是短期信号
3. 板块轮动有周期性（金融→消费→科技→周期）
4. 强势板块中的个股更容易盈利

输出：
- hot_sectors: 当前强势板块列表
- sector_scores: 各板块评分
- recommended_sectors: 推荐的3-5个板块
"""

from __future__ import annotations

from typing import Any

import akshare as ak
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

# 行业分类映射（申万一级行业）
SW_INDUSTRY_MAP = {
    "银行": "金融",
    "非银金融": "金融",
    "房地产": "金融",
    "食品饮料": "消费",
    "家用电器": "消费",
    "汽车": "消费",
    "纺织服饰": "消费",
    "商贸零售": "消费",
    "社会服务": "消费",
    "农林牧渔": "消费",
    "医药生物": "消费",
    "电子": "科技",
    "计算机": "科技",
    "通信": "科技",
    "传媒": "科技",
    "国防军工": "科技",
    "电力设备": "制造",
    "机械设备": "制造",
    "基础化工": "周期",
    "有色金属": "周期",
    "钢铁": "周期",
    "煤炭": "周期",
    "石油石化": "周期",
    "建筑材料": "周期",
    "建筑装饰": "周期",
    "公用事业": "公用",
    "交通运输": "公用",
    "环保": "公用",
}


class SectorRotation:
    """板块轮动检测器。"""

    @staticmethod
    def get_industry_category(industry: str) -> str:
        """获取行业所属大类分类。

        P2 修复：封装 SW_INDUSTRY_MAP 的访问，
        外部模块通过此方法获取行业分类而非直接访问模块内部常量。
        """
        return SW_INDUSTRY_MAP.get(industry, "")

    """板块轮动检测器。

    使用东方财富行业板块数据，综合判断板块强弱：
    1. 近5日涨跌幅排名
    2. 近20日涨跌幅排名
    3. 资金净流入排名
    4. 成交量放大程度
    """

    def __init__(self):
        self._cache: dict[str, Any] = {}
        self._cache_date: str = ""

    async def detect(self, top_n: int = 5) -> dict[str, Any]:
        """检测当前强势板块。

        Returns:
            dict with: hot_sectors, sector_scores, recommended_sectors, reason
        """
        from datetime import date

        today = str(date.today())
        if self._cache_date == today and self._cache:
            return self._cache

        try:
            # 获取行业板块行情
            df = ak.stock_board_industry_name_em()
            if df.empty:
                return self._fallback()

            # 计算板块评分
            scores = self._score_sectors(df)

            # 选出强势板块
            top_sectors = sorted(scores.items(), key=lambda x: x[1]["total"], reverse=True)[:top_n]

            recommended = [s[0] for s in top_sectors]
            # 映射到大类
            categories = set()
            for s in recommended:
                cat = SW_INDUSTRY_MAP.get(s, "其他")
                categories.add(cat)

            result = {
                "hot_sectors": recommended,
                "sector_scores": dict(top_sectors),
                "recommended_sectors": recommended[:3],
                "categories": list(categories),
                "reason": f"强势板块: {', '.join(recommended[:3])} (大类: {', '.join(categories)})",
            }
            self._cache = result
            self._cache_date = today
            return result

        except Exception as e:
            logger.error(f"板块轮动检测失败: {e}")
            return self._fallback()

    def _score_sectors(self, df: pd.DataFrame) -> dict[str, dict[str, float]]:
        """计算板块评分"""
        scores = {}
        for _, row in df.iterrows():
            name = row.get("板块名称", "")
            if not name:
                continue

            # 涨跌幅
            pct_change = float(row.get("板块涨跌幅", 0) or 0)
            # 主力净流入
            main_net = float(row.get("主力净流入", 0) or 0) / 1e8  # 转亿

            # 综合评分：涨跌幅60% + 资金流入40%
            # 涨跌幅归一化到0-1
            pct_score = (
                min(max(pct_change / 5, 0), 1) if pct_change > 0 else max(pct_change / 5, -1)
            )
            # 资金流归一化
            flow_score = min(max(main_net / 10, -1), 1)

            total = pct_score * 0.6 + flow_score * 0.4

            scores[name] = {
                "pct_change": round(pct_change, 2),
                "main_net_billion": round(main_net, 2),
                "pct_score": round(pct_score, 2),
                "flow_score": round(flow_score, 2),
                "total": round(total, 4),
            }

        return scores

    async def filter_stocks_by_sector(
        self, symbols: list[str], hot_sectors: list[str]
    ) -> list[str]:
        """根据强势板块筛选股票。

        Args:
            symbols: 待筛选的股票代码列表
            hot_sectors: 强势板块名称列表

        Returns:
            list[str]: 属于强势板块的股票代码
        """
        from gugu.filters.industry_constraint import IndustryConstraint

        industry_checker = IndustryConstraint()
        filtered = []
        for symbol in symbols:
            try:
                industry = industry_checker.get_industry(symbol)
                # 检查是否属于强势板块或相关大类
                cat = SW_INDUSTRY_MAP.get(industry, "其他")
                if industry in hot_sectors or cat in [
                    SW_INDUSTRY_MAP.get(s, "") for s in hot_sectors
                ]:
                    filtered.append(symbol)
            except Exception:
                filtered.append(symbol)  # 获取失败时不过滤

        return filtered

    def _fallback(self) -> dict[str, Any]:
        return {
            "hot_sectors": [],
            "sector_scores": {},
            "recommended_sectors": [],
            "categories": [],
            "reason": "板块数据获取失败",
        }

    # ════════════════════════════════════════════════════════════
    # 高低切换板块轮动检测（v0.4.5 新增）
    # ════════════════════════════════════════════════════════════
    # 来源：实战派「双阴钱袋子」战法中的高低切换理念
    # 核心逻辑：
    #   - 高位板块（涨幅已大 + 资金流出）→ 规避，不追高
    #   - 低位板块（跌幅已深 + 资金流入）→ 布局，等反转
    #   - 资金从高位流向低位时 → 板块轮动信号

    # 高位板块判定阈值
    HIGH_SECTOR_PCT_THRESHOLD = 5.0   # 20日涨幅 > 5% 视为高位
    LOW_SECTOR_PCT_THRESHOLD = -3.0   # 20日涨幅 < -3% 视为低位

    async def detect_high_low_rotation(self) -> dict[str, Any]:
        """检测高低切换板块轮动。

        识别两类板块：
        - overextended_sectors: 高位板块（涨幅大 + 主力净流出），资金可能撤出
        - undervalued_sectors: 低位板块（跌幅深 + 主力净流入），资金可能进入

        Returns:
            dict with: overextended_sectors, undervalued_sectors,
                       rotation_signal, reason
        """
        from datetime import date

        today = str(date.today())
        cache_key = f"highlow_{today}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            df = ak.stock_board_industry_name_em()
            if df.empty:
                return self._highlow_fallback()

            scores = self._score_sectors(df)
            overextended = []
            undervalued = []

            for name, score in scores.items():
                pct = score.get("pct_change", 0)
                flow = score.get("main_net_billion", 0)

                # 高位板块：涨幅大 + 主力净流出（资金撤退信号）
                if pct > self.HIGH_SECTOR_PCT_THRESHOLD and flow < 0:
                    overextended.append({
                        "sector": name,
                        "pct_change": pct,
                        "main_net_billion": flow,
                        "category": SW_INDUSTRY_MAP.get(name, "其他"),
                    })

                # 低位板块：跌幅深 + 主力净流入（资金抄底信号）
                elif pct < self.LOW_SECTOR_PCT_THRESHOLD and flow > 0:
                    undervalued.append({
                        "sector": name,
                        "pct_change": pct,
                        "main_net_billion": flow,
                        "category": SW_INDUSTRY_MAP.get(name, "其他"),
                    })

            # 按涨幅排序
            overextended.sort(key=lambda x: x["pct_change"], reverse=True)
            undervalued.sort(key=lambda x: x["pct_change"])

            # 轮动信号：同时存在高位流出和低位流入
            rotation_signal = bool(overextended and undervalued)

            result = {
                "overextended_sectors": overextended[:5],
                "undervalued_sectors": undervalued[:5],
                "rotation_signal": rotation_signal,
                "reason": (
                    f"高位板块{len(overextended)}个（资金撤出），"
                    f"低位板块{len(undervalued)}个（资金流入），"
                    f"{'高低切换进行中' if rotation_signal else '无明显切换'}"
                ),
            }

            self._cache[cache_key] = result
            return result

        except Exception as e:
            logger.error(f"高低切换检测失败: {e}")
            return self._highlow_fallback()

    async def check_high_low_filter(self, symbol: str) -> dict[str, Any]:
        """检查个股是否处于高位/低位板块，用于信号过滤。

        高位板块的买入信号应被拒绝（不追高），
        低位板块的买入信号应被优先考虑（等反转）。

        Args:
            symbol: 股票代码

        Returns:
            dict with: allow_buy, is_overextended, is_undervalued,
                       sector, category, reason
        """
        from gugu.filters.industry_constraint import IndustryConstraint

        try:
            rotation = await self.detect_high_low_rotation()
            overextended_names = {s["sector"] for s in rotation.get("overextended_sectors", [])}
            undervalued_names = {s["sector"] for s in rotation.get("undervalued_sectors", [])}
            overextended_cats = {s["category"] for s in rotation.get("overextended_sectors", [])}
            undervalued_cats = {s["category"] for s in rotation.get("undervalued_sectors", [])}

            ic = IndustryConstraint()
            industry = ic.get_industry(symbol)
            category = SW_INDUSTRY_MAP.get(industry, "其他")

            is_overextended = industry in overextended_names or category in overextended_cats
            is_undervalued = industry in undervalued_names or category in undervalued_cats

            if is_overextended:
                return {
                    "allow_buy": False,
                    "is_overextended": True,
                    "is_undervalued": False,
                    "sector": industry,
                    "category": category,
                    "reason": f"板块「{industry}」处于高位（涨幅大+资金流出），规避追高",
                }
            elif is_undervalued:
                return {
                    "allow_buy": True,
                    "is_overextended": False,
                    "is_undervalued": True,
                    "sector": industry,
                    "category": category,
                    "reason": f"板块「{industry}」处于低位（跌幅深+资金流入），适合低吸",
                }
            else:
                return {
                    "allow_buy": True,
                    "is_overextended": False,
                    "is_undervalued": False,
                    "sector": industry,
                    "category": category,
                    "reason": f"板块「{industry}」处于正常区间",
                }

        except Exception as e:
            logger.warning(f"高低切换过滤检查失败({symbol}): {e}")
            return {
                "allow_buy": True,
                "is_overextended": False,
                "is_undervalued": False,
                "sector": "",
                "category": "",
                "reason": f"高低切换检查失败: {e}",
            }

    def _highlow_fallback(self) -> dict[str, Any]:
        return {
            "overextended_sectors": [],
            "undervalued_sectors": [],
            "rotation_signal": False,
            "reason": "高低切换数据获取失败",
        }
