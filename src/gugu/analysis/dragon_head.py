"""龙头识别器（DragonHeadDetector）。

钱多多龙头战法核心：
- 有总龙头，就不要做分龙头
- 有连板的龙头，就不要做反包的龙头
- 有主升的龙头，就不要做反抽的龙头
- 有主线的龙头，就不要做支线的龙头
- 有市场选出的龙头，就不要做自己选的龙头

四梁八柱：
- 中军：大市值同板块趋势股，稳步向上（很少涨停但趋势不破）
- 跟随龙：每天至少1-2只同板块涨停
- 老龙：上一周期龙头高位震荡不破支撑
- 补涨：轮动涨停的膜拜者

情绪周期判断：
- 健康：中军向上 + 跟随龙涨停 + 老龙高位震荡
- 衰退：中军上升无力 + 跟随龙不涨停 + 龙头独木难支
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


class DragonHeadDetector:
    """兼容类接口，封装现有跨股龙头识别函数。

    输出明确标注为 heuristic/unverified，不参与策略准入或 verified 升级。
    """

    def __init__(
        self,
        sector_map: dict[str, list[str]] | None = None,
        lookback: int = 20,
        strong_day_pct: float = 0.07,
    ):
        self.sector_map = sector_map
        self.lookback = lookback
        self.strong_day_pct = strong_day_pct

    def detect(self, stock_data: dict[str, pd.DataFrame]) -> dict[str, dict]:
        """Run heuristic detection and attach evidence metadata."""
        result = identify_dragon_head(
            stock_data, self.sector_map, self.lookback, self.strong_day_pct
        )
        coverage = len(stock_data)
        asof = max(
            (
                str(df["date"].iloc[-1])
                for df in stock_data.values()
                if not df.empty and "date" in df.columns
            ),
            default="",
        )
        for info in result.values():
            info.update(
                {
                    "evidence_status": "heuristic",
                    "data_coverage": coverage,
                    "sample_count": coverage,
                    "asof": asof,
                    "roles_complete": bool(info.get("anchor_symbol") or info.get("followers")),
                }
            )
        return result


__all__ = ["DragonHeadDetector", "identify_dragon_head"]
SECTOR_MAP: dict[str, list[str]] = {
    "白酒": ["600519", "000858", "000568", "002304"],
    "新能源": ["300750", "002594", "601012", "600438"],
    "半导体": ["002371", "603501", "688981", "688012"],
    "医药": ["600276", "300760", "300015", "000538"],
    "金融科技": ["300033", "002230", "300059"],
    "消费电子": ["002475", "601138", "002241"],
}


def identify_dragon_head(
    stock_data: dict[str, pd.DataFrame],
    sector_map: dict[str, list[str]] | None = None,
    lookback: int = 20,
    strong_day_pct: float = 0.07,
) -> dict[str, dict]:
    """识别每个板块的龙头股。

    Args:
        stock_data: {symbol: DataFrame} 所有股票的日线数据
        sector_map: 板块映射，默认使用 SECTOR_MAP
        lookback: 回溯天数

    Returns:
        {symbol: dragon_head_info} 每个股票的龙头分析结果
    """
    if sector_map is None:
        sector_map = SECTOR_MAP

    result: dict[str, dict] = {}

    for sector_name, symbols in sector_map.items():
        # 只分析有数据的股票
        active = {s: stock_data[s] for s in symbols if s in stock_data}
        if len(active) < 2:
            continue

        # ── 计算每只股票的龙头指标 ──────────────────
        scores = {}
        for sym, df in active.items():
            if len(df) < lookback:
                continue
            close = df["close"].values
            volume = df["volume"].values

            # 1. 近期涨幅（相对强度）
            ret_5d = (close[-1] / close[-min(5, len(close))] - 1) if len(close) >= 5 else 0
            ret_10d = (close[-1] / close[-min(10, len(close))] - 1) if len(close) >= 10 else 0

            # 2. 强势日检测（默认涨幅 > 7%，由配置/Detector 参数统一）
            daily_ret = np.diff(close) / close[:-1]
            limit_up_days = (
                int(np.sum(daily_ret[-lookback:] > strong_day_pct))
                if len(daily_ret) >= lookback
                else 0
            )

            # 3. 量能扩张（近5日量 vs 20日均量）
            vol_5d = np.mean(volume[-5:]) if len(volume) >= 5 else 0
            vol_20d = np.mean(volume[-20:]) if len(volume) >= 20 else 1
            vol_expand = vol_5d / vol_20d if vol_20d > 0 else 1

            # 4. 趋势强度（收盘价 vs MA20）
            ma20 = np.mean(close[-20:]) if len(close) >= 20 else close[-1]
            trend_strength = (close[-1] / ma20 - 1) if ma20 > 0 else 0

            # 5. 市值因子（是否中军候选：股价高 + 成交额大）
            amount = df["amount"].values if "amount" in df.columns else volume * close
            avg_amount = np.mean(amount[-20:]) if len(amount) >= 20 else 0
            is_anchor = avg_amount > 1e8  # 日均成交额 > 1亿

            scores[sym] = {
                "ret_5d": ret_5d,
                "ret_10d": ret_10d,
                "limit_up_days": limit_up_days,
                "vol_expand": float(vol_expand),
                "trend_strength": float(trend_strength),
                "is_anchor": is_anchor,
                "avg_amount": float(avg_amount),
                "close": float(close[-1]),
                "sector": sector_name,
            }

        if not scores:
            continue

        # ── 确定龙头：综合评分最高的股票 ────────────
        # 龙头评分 = 涨幅 + 连板 + 量能 + 趋势
        for _sym, s in scores.items():
            s["dragon_score"] = (
                s["ret_5d"] * 30
                + s["ret_10d"] * 20
                + s["limit_up_days"] * 0.15
                + s["vol_expand"] * 0.15
                + s["trend_strength"] * 0.20
            )

        # 排序确定龙头
        ranked = sorted(scores.items(), key=lambda x: x[1]["dragon_score"], reverse=True)
        dragon = ranked[0][0] if ranked else None
        followers = [s for s, _ in ranked[1:4]] if len(ranked) > 1 else []

        # 找中军：大市值趋势股
        anchors = [
            s for s, sc in scores.items() if sc["is_anchor"] and sc["trend_strength"] > -0.02
        ]
        anchor = anchors[0] if anchors else None

        # ── 情绪周期判断 ──────────────────────────
        dragon_info = scores.get(dragon, {})
        anchor_info = scores.get(anchor, {}) if anchor else {}

        # 龙头情绪：连板 + 量能扩张
        dragon_healthy = (
            dragon_info.get("limit_up_days", 0) >= 1 or dragon_info.get("trend_strength", 0) > 0.03
        )

        # 中军情绪：趋势向上
        anchor_healthy = anchor_info.get("trend_strength", -1) > 0

        # 跟随龙情绪：有至少1只跟涨
        follower_count = sum(1 for s in followers if scores.get(s, {}).get("ret_5d", -1) > 0.01)

        sentiment = (
            "healthy"
            if (dragon_healthy and anchor_healthy and follower_count >= 1)
            else "weakening"
        )

        # ── 组装结果 ──────────────────────────────
        for sym, s in scores.items():
            is_dragon = sym == dragon
            is_follower = sym in followers
            is_anchor = sym == anchor

            result[sym] = {
                "sector": sector_name,
                "is_dragon": is_dragon,
                "is_follower": is_follower,
                "is_anchor": is_anchor,
                "dragon_score": s["dragon_score"],
                "limit_up_days": s["limit_up_days"],
                "vol_expand": s["vol_expand"],
                "trend_strength": s["trend_strength"],
                "ret_5d": s["ret_5d"],
                "sentiment": sentiment,
                "follower_count": follower_count,
                "dragon_symbol": dragon,
                "anchor_symbol": anchor,
                "followers": followers,
                "dragon_healthy": dragon_healthy,
                "anchor_healthy": anchor_healthy,
            }

    return result
