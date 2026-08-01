"""多视角共识注入：将 28 本书的多视角分析结果注入信号。

从 BookPerspectiveRouter 获取多视角分析，计算共识指标并更新 mentor_perspectives 字段。
高共识增强置信度，低共识降低置信度。
"""

from __future__ import annotations

from typing import Any

from gugu.utils.log import get_logger

logger = get_logger()


def inject_perspective_consensus(book_router: Any, signal: dict[str, Any]) -> dict[str, Any]:
    """将多视角分析结果注入信号。

    调用 book_router.get_advice(signal) 获取多视角分析，
    计算共识指标并更新 mentor_perspectives 字段。
    高共识增强置信度，低共识降低置信度。

    Args:
        book_router: BookPerspectiveRouter 实例或 None
        signal: 信号 dict（需包含 direction, confidence 等字段）

    Returns:
        更新后的信号 dict（含 mentor_perspectives 字段）
    """
    if book_router is None:
        return signal

    try:
        advice = book_router.get_advice(signal)
        perspectives = advice.get("perspectives", [])
        if not perspectives:
            return signal

        consensus = advice.get("consensus", {})
        buy_pct = consensus.get("buy_pct", 0)
        sell_pct = consensus.get("sell_pct", 0)
        hold_pct = consensus.get("hold_pct", 0)

        # 分类视角
        buy_perspectives: list[str] = []
        sell_perspectives: list[str] = []
        hold_perspectives: list[str] = []
        top_reasons: list[dict] = []

        for p in perspectives:
            action = p.get("action", "")
            name = p.get("name", "")
            reason = p.get("reason", "")
            if action == "buy":
                buy_perspectives.append(name)
            elif action == "sell":
                sell_perspectives.append(name)
            else:
                hold_perspectives.append(name)

            # 收集置信度最高的视角原因
            top_reasons.append(
                {
                    "perspective": name,
                    "reason": reason,
                }
            )

        # 取 top-3 理由
        top_reasons = sorted(
            top_reasons,
            key=lambda x: next(
                (p.get("confidence", 0) for p in perspectives if p.get("name") == x["perspective"]),
                0,
            ),
            reverse=True,
        )[:3]

        # 确定共识标签
        if buy_pct >= 0.7:
            consensus_label = "strong_buy"
        elif buy_pct >= 0.5 and buy_pct > sell_pct:
            consensus_label = "buy"
        elif sell_pct >= 0.7:
            consensus_label = "strong_sell"
        elif sell_pct >= 0.5 and sell_pct > buy_pct:
            consensus_label = "sell"
        elif abs(buy_pct - sell_pct) < 0.2 and buy_pct > 0 and sell_pct > 0:
            consensus_label = "conflict"
        else:
            consensus_label = "conflict"

        mentor_perspectives = {
            "analyzed_count": len(perspectives),
            "buy_agreement_pct": buy_pct,
            "sell_agreement_pct": sell_pct,
            "hold_agreement_pct": hold_pct,
            "buy_perspectives": buy_perspectives,
            "sell_perspectives": sell_perspectives,
            "hold_perspectives": hold_perspectives,
            "top_reasons": top_reasons,
            "consensus": consensus_label,
        }

        signal["mentor_perspectives"] = mentor_perspectives

        # 高共识（>70%同意买入）→ 增强置信度
        if buy_pct > 0.70:
            current_conf = float(signal.get("confidence", 0.5))
            signal["confidence"] = min(1.0, current_conf + 0.1)
            logger.info(
                "[wisdom-perspective] 高共识({:.0%}买入)增强置信度: {:.2f} → {:.2f}",
                buy_pct,
                current_conf,
                signal["confidence"],
            )
        # 低共识（<30%同意买入）→ 降低置信度
        elif buy_pct < 0.30 and signal.get("direction") == "buy":
            current_conf = float(signal.get("confidence", 0.5))
            signal["confidence"] = max(0.1, current_conf - 0.1)
            logger.info(
                "[wisdom-perspective] 低共识({:.0%}买入)降低置信度: {:.2f} → {:.2f}",
                buy_pct,
                current_conf,
                signal["confidence"],
            )

    except Exception as e:
        logger.warning("[wisdom-perspective] 多视角分析异常，跳过: {}", e)

    return signal
