"""十大视角分析引擎——核心编排器。

负责：
1. 构建 StockDataContext（从多种数据源聚合）
2. 调用所有评分函数
3. 汇总结果 + 生成综合判定
4. 提供批量分析接口
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from gugu.analysis.perspectives.data_types import (
    ALL_PERSPECTIVES,
    DEFAULT_WEIGHTS,
    PerspectiveAnalysisResult,
    PerspectiveScore,
    StockDataContext,
)
from gugu.analysis.perspectives.scorer import SCORERS
from gugu.utils.log import get_logger

logger = get_logger()


class PerspectiveEngine:
    """十大视角分析引擎。

    用法：
        engine = PerspectiveEngine()
        ctx = engine.build_context(symbol="600519", name="贵州茅台", price=1800, ...)
        result = await engine.analyze(ctx)
    """

    def __init__(
        self,
        perspectives: list[str] | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        """
        Args:
            perspectives: 启用的视角列表（None=全部）
            weights: 自定义权重（None=默认权重）
        """
        self._perspectives = perspectives or ALL_PERSPECTIVES
        self._weights = weights or DEFAULT_WEIGHTS

    def analyze(self, ctx: StockDataContext) -> PerspectiveAnalysisResult:
        """执行十大视角综合分析。

        同步方法——所有评分函数均为纯计算，无 I/O。
        """
        scores: list[PerspectiveScore] = []

        for perspective in self._perspectives:
            scorer = SCORERS.get(perspective)
            if scorer is None:
                logger.warning(f"未知视角: {perspective}，跳过")
                continue
            try:
                score = scorer(ctx)
                # 应用自定义权重
                if perspective in self._weights:
                    score.weight = self._weights[perspective]
                scores.append(score)
            except Exception as e:
                logger.error(f"视角 {perspective} 评分失败: {e}")
                # 失败时给一个中性低置信分
                scores.append(PerspectiveScore(
                    perspective=perspective,
                    score=5.0,
                    weight=self._weights.get(perspective, 0.1),
                    confidence=0.3,
                    reasoning=f"评分异常: {e}",
                    verdict="评分异常",
                ))

        return self._aggregate(ctx, scores)

    def analyze_batch(
        self,
        contexts: list[StockDataContext],
        max_concurrency: int = 5,
    ) -> list[PerspectiveAnalysisResult]:
        """批量分析多只股票（同步，CPU密集型）。

        由于所有评分函数均为纯计算，直接使用列表推导式。
        """
        return [self.analyze(ctx) for ctx in contexts]

    def _aggregate(
        self,
        ctx: StockDataContext,
        scores: list[PerspectiveScore],
    ) -> PerspectiveAnalysisResult:
        """聚合所有视角评分，生成综合结果。"""
        if not scores:
            return PerspectiveAnalysisResult(
                symbol=ctx.symbol,
                name=ctx.name,
                price=ctx.price,
                overall_verdict="无评分数据",
            )

        # 加权综合评分
        total_weight = sum(s.weight * s.confidence for s in scores)
        if total_weight > 0:
            composite = sum(s.weighted_score for s in scores) / total_weight
        else:
            composite = 5.0

        # 最低分（木桶效应）
        min_score = min(s.score for s in scores)
        min_perspective = next(s.perspective for s in scores if s.score == min_score)

        # 通过数和致命缺陷数
        passed_count = sum(1 for s in scores if s.passed)
        fatal_count = sum(1 for s in scores if s.is_fatal)

        # 收集优势和风险
        top_strengths: list[str] = []
        top_risks: list[str] = []
        for s in sorted(scores, key=lambda x: x.score, reverse=True):
            if s.score >= 7 and s.key_factors:
                top_strengths.append(f"[{s.perspective}] {s.key_factors[0]}")
            if s.score < 5 and s.risk_flags:
                top_risks.append(f"[{s.perspective}] {s.risk_flags[0]}")

        # 操作建议
        suggestion = self._generate_suggestion(composite, min_score, fatal_count, ctx)
        entry_price, stop_loss, target = self._calc_prices(ctx, composite)

        # 综合推理
        reasoning = self._build_overall_reasoning(scores, composite, min_score, min_perspective)

        # 综合判定
        if composite >= 8 and fatal_count == 0:
            verdict = "强烈推荐买入"
        elif composite >= 7 and fatal_count == 0:
            verdict = "推荐买入"
        elif composite >= 6 and fatal_count == 0:
            verdict = "可考虑买入"
        elif composite >= 5:
            verdict = "中性观望"
        elif fatal_count > 0:
            verdict = f"否决（{fatal_count}个致命缺陷）"
        else:
            verdict = "不建议买入"

        return PerspectiveAnalysisResult(
            symbol=ctx.symbol,
            name=ctx.name,
            price=ctx.price,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
            scores=scores,
            composite_score=round(composite, 2),
            min_score=round(min_score, 1),
            min_perspective=min_perspective,
            passed_count=passed_count,
            fatal_count=fatal_count,
            overall_verdict=verdict,
            overall_reasoning=reasoning,
            top_strengths=top_strengths[:5],
            top_risks=top_risks[:5],
            suggestion=suggestion,
            entry_price=entry_price,
            stop_loss_price=stop_loss,
            target_price=target,
        )

    def _generate_suggestion(
        self,
        composite: float,
        min_score: float,
        fatal_count: int,
        ctx: StockDataContext,
    ) -> str:
        """生成操作建议。"""
        if fatal_count > 0:
            return f"不建议操作：存在{fatal_count}个致命缺陷视角，风险收益比不佳"
        if composite >= 8:
            return f"强烈建议买入：当前价{ctx.price}附近可建仓，止损参考MA20"
        if composite >= 7:
            return f"建议买入：当前价{ctx.price}附近可轻仓试探"
        if composite >= 6:
            return f"可考虑：等待更好入场价位或确认信号"
        if min_score < 5:
            return f"等待：最低分视角仅{min_score:.1f}分，需改善后再评估"
        return "观望：综合评分不足，等待更好机会"

    def _calc_prices(
        self,
        ctx: StockDataContext,
        composite: float,
    ) -> tuple[float | None, float | None, float | None]:
        """计算入场价、止损价、目标价。"""
        if ctx.price <= 0:
            return None, None, None

        # 入场价：当前价或 MA20（取更优）
        entry = ctx.price
        if ctx.ma20 > 0 and ctx.ma20 < ctx.price * 0.97:
            entry = ctx.ma20  # 等回踩 MA20

        # 止损价：MA20 × 0.97 或 ATR 止损
        stop_loss = ctx.ma20 * 0.97 if ctx.ma20 > 0 else ctx.price * 0.95
        if ctx.atr14 > 0:
            atr_stop = ctx.price - 2 * ctx.atr14
            if atr_stop > stop_loss:
                stop_loss = atr_stop

        # 目标价：盈亏比 ≥ 2
        risk = entry - stop_loss
        target = entry + risk * 2.5 if risk > 0 else entry * 1.10

        return (
            round(entry, 2),
            round(stop_loss, 2),
            round(target, 2),
        )

    def _build_overall_reasoning(
        self,
        scores: list[PerspectiveScore],
        composite: float,
        min_score: float,
        min_perspective: str,
    ) -> str:
        """构建综合推理链。"""
        # 找出最强和最弱视角
        sorted_scores = sorted(scores, key=lambda s: s.score, reverse=True)
        top3 = [f"{s.perspective}({s.score:.0f}分)" for s in sorted_scores[:3]]
        bottom3 = [f"{s.perspective}({s.score:.0f}分)" for s in sorted_scores[-3:]]

        return (
            f"综合{composite:.1f}分，最强: {', '.join(top3)}；"
            f"最弱: {', '.join(bottom3)}；"
            f"木桶短板: {min_perspective}({min_score:.1f}分)"
        )
