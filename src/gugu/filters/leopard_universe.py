"""豹哥三要素选股池过滤器 — 高流动性 + 大资金承载 + 活跃博弈。

来源：豹哥战法（微信公众号文章 https://mp.weixin.qq.com/s/guNx28XZgrjSpPy2Ug6x7Q）

三要素阈值（文章原文）：
1. 换手率 ≥ 15%   — 标的足够活跃，资金博弈明显，避免冷门股
2. 成交额 ≥ 10亿  — 标的规模大，降低遭遇控庄的概率
3. 流通市值 ≥ 70亿 — 容量足够，散户参与感好

═══════════════════════════════════════════════════════════
第一性原理：三要素是「选股池准入」，不是「择时信号」
═══════════════════════════════════════════════════════════
文章核心观点：三要素层层筛选，从全市场 5000+ → 165 只 → 104 只 → 86 只，
帮助新手快速缩小到可操作范围。三要素本身不产生 alpha，只解决"在哪选"的问题，
不解决"何时买"的问题（择时由 box_breakout/pullback_low/double_yin 等策略承担）。

与 leopard_three_factors 策略的根本差异：
- leopard_three_factors（v0.4.2 已禁用）：把三要素和放量突破绑定成完整策略 → 回测夏普 -0.11
- LeopardUniverseFilter（本模块）：纯选股池过滤器，不绑定择时 → 可作为任何策略的前置预筛

使用场景适配：
- 适合与 box_breakout（突破追势）配合：三要素选出的活跃大盘股更适合趋势突破
- 不适合与 pullback_low/double_yin（缩量低吸）配合：三要素要求换手率≥15%（高度活跃），
  而低吸策略要求换手率 3-15%（缩量回踩），两者逻辑冲突
- 因此默认关闭，仅在趋势市/突破策略启用时手动开启
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


class LeopardUniverseFilter:
    """豹哥三要素选股池过滤器。

    按文章理念，三要素作为全市场预筛器，过滤出"大资金进得来、出得去"的活跃大盘股池。
    不产生买卖信号，只缩小候选范围。

    阈值（文章原文，可通过 params 覆盖）：
        turnover_min: 换手率下限（%，默认 15.0）
        amount_min:    成交额下限（元，默认 1e9 = 10亿）
        circ_mv_min:   流通市值下限（元，默认 7e9 = 70亿）
    """

    # 文章原文阈值（类常量，作为默认值和文档锚点）
    DEFAULT_TURNOVER_MIN = 15.0  # 换手率 ≥ 15%
    DEFAULT_AMOUNT_MIN = 1_000_000_000  # 成交额 ≥ 10亿（1e9 元）
    DEFAULT_CIRC_MV_MIN = 7_000_000_000  # 流通市值 ≥ 70亿（7e9 元）

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self._params = params or {}
        self.turnover_min = float(self._params.get("turnover_min", self.DEFAULT_TURNOVER_MIN))
        self.amount_min = float(self._params.get("amount_min", self.DEFAULT_AMOUNT_MIN))
        self.circ_mv_min = float(self._params.get("circ_mv_min", self.DEFAULT_CIRC_MV_MIN))

    def filter(self, market_df: pd.DataFrame) -> pd.DataFrame:
        """过滤出符合三要素的股票池。

        Args:
            market_df: 全市场快照，需包含列：
                - turnover_pct: 换手率（%）
                - amount: 成交额（元）
                - circ_mv: 流通市值（元）

        Returns:
            符合三要素的股票池 DataFrame（保留原索引顺序）

        数据缺失处理：
        - 若 turnover_pct/circ_mv 列不存在或全为 0（数据源未返回），
          仅用成交额过滤（三要素降级为单要素，并在日志中警告）
        - 文章理念：三要素层层筛选，缺一不可；但数据不可用时不应阻塞选股流程
        """
        if market_df.empty:
            return market_df

        df = market_df.copy()
        before = len(df)

        # 要素1：成交额 ≥ 10亿（核心要素，数据最可靠）
        if "amount" in df.columns:
            df = df[df["amount"] >= self.amount_min]
        else:
            logger.warning("豹哥三要素：快照缺少 amount 列，跳过成交额过滤")

        # 要素2：换手率 ≥ 15%
        has_turnover = "turnover_pct" in df.columns and not df["turnover_pct"].eq(0).all()
        if has_turnover:
            df = df[df["turnover_pct"] >= self.turnover_min]
        else:
            logger.warning(
                "豹哥三要素：快照缺少 turnover_pct 列或全为 0，降级为仅成交额+流通市值过滤"
            )

        # 要素3：流通市值 ≥ 70亿
        has_circ_mv = "circ_mv" in df.columns and not df["circ_mv"].eq(0).all()
        if has_circ_mv:
            df = df[df["circ_mv"] >= self.circ_mv_min]
        else:
            logger.warning("豹哥三要素：快照缺少 circ_mv 列或全为 0，降级为仅成交额+换手率过滤")

        after = len(df)
        logger.info(
            f"豹哥三要素选股池过滤: {before} → {after} 只 "
            f"(换手率≥{self.turnover_min}% + 成交额≥{self.amount_min / 1e8:.0f}亿 "
            f"+ 流通市值≥{self.circ_mv_min / 1e8:.0f}亿)"
        )
        return df.reset_index(drop=True)

    def describe(self) -> dict[str, Any]:
        """返回过滤器描述（用于飞书看板/调试）。"""
        return {
            "name": "豹哥三要素选股池",
            "turnover_min": self.turnover_min,
            "amount_min_billion": self.amount_min / 1e8,
            "circ_mv_min_billion": self.circ_mv_min / 1e8,
            "source": "豹哥战法（微信公众号文章）",
            "positioning": "选股池准入过滤器（非择时信号）",
        }
