"""M因子市场维度引擎（V5框架集成）。

基于V5.5.1回测验证的分档系数制，计算市场综合维度用于仓位控制。

核心逻辑：
- M_basic：四大指数（上证30%+深成30%+创业板20%+科创50 20%）5日动量评分
- M_sector：个股所在行业动量评分（1d/5d/20d加权）
- 综合市场维度 = M_basic系数 × M_sector系数 × 10
- 综合分<4 → 触发收紧：仓位上限×60%，单只上限×15%

分档系数（V5.5.1回测校准）：
  M_basic ≥5 → A档×1.0（强势，20日后+0.82%/胜率57%）
  M_basic 3-4 → B档×0.8（过渡待确认，20日后+0.11%/胜率45.9%——最差档）
  M_basic 1-2 → C档×0.6（偏弱）
  M_basic ≤0 → D档×0.4（弱势，总敞口≤30%）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

INDEX_WEIGHTS = {
    "sh": 0.30,
    "sz": 0.30,
    "cy": 0.20,
    "kc": 0.20,
}


@dataclass
class MFactorResult:
    m_basic: float
    m_basic_tier: str
    m_basic_coefficient: float
    m_sector_coefficient: float
    m_sector_tier: str
    comprehensive_score: float
    position_action: str
    total_exposure_limit: float
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "m_basic": round(self.m_basic, 1),
            "m_basic_tier": self.m_basic_tier,
            "m_basic_coefficient": self.m_basic_coefficient,
            "m_sector_coefficient": self.m_sector_coefficient,
            "m_sector_tier": self.m_sector_tier,
            "comprehensive_score": round(self.comprehensive_score, 1),
            "position_action": self.position_action,
            "total_exposure_limit": self.total_exposure_limit,
            "timestamp": self.timestamp,
            "details": self.details,
        }


def _score_daily_change(pct: float) -> int:
    if pct > 0.5:
        return 1
    if pct < -0.5:
        return -1
    return 0


def calc_m_basic_from_df(index_data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """从DataFrame计算M_basic。

    Args:
        index_data: {指数名: 日K线DataFrame}，需包含close列。
    """
    all_scores: dict[str, dict[str, Any]] = {}

    for name in INDEX_WEIGHTS:
        df = index_data.get(name)
        if df is None or len(df) < 6:
            all_scores[name] = {
                "daily_scores": [0] * 5,
                "total": 0,
                "changes": [0.0] * 5,
            }
            continue

        recent = df.tail(6)
        closes = recent["close"].values.astype(float)
        changes = []
        daily_scores = []
        for i in range(1, len(closes)):
            prev_c = closes[i - 1]
            chg = (closes[i] - prev_c) / prev_c * 100 if prev_c > 0 else 0.0
            changes.append(chg)
            daily_scores.append(_score_daily_change(chg))

        while len(daily_scores) < 5:
            daily_scores.insert(0, 0)
            changes.insert(0, 0.0)

        daily_scores = daily_scores[-5:]
        changes = changes[-5:]

        all_scores[name] = {
            "daily_scores": daily_scores,
            "total": sum(daily_scores),
            "changes": [round(c, 2) for c in changes],
        }

    m_basic_raw = sum(all_scores[name]["total"] * INDEX_WEIGHTS[name] for name in INDEX_WEIGHTS)

    same_dir_up = 0
    same_dir_down = 0
    for day in range(5):
        day_up = sum(
            1
            for name in INDEX_WEIGHTS
            if day < len(all_scores[name]["daily_scores"])
            and all_scores[name]["daily_scores"][day] > 0
        )
        day_down = sum(
            1
            for name in INDEX_WEIGHTS
            if day < len(all_scores[name]["daily_scores"])
            and all_scores[name]["daily_scores"][day] < 0
        )
        if day_up >= 3:
            same_dir_up += 1
        if day_down >= 3:
            same_dir_down += 1

    bonus = 0
    if same_dir_up >= 4:
        bonus = 1
    elif same_dir_down >= 4:
        bonus = -1

    m_basic = round(m_basic_raw + bonus, 1)

    if m_basic >= 5:
        tier = "A档·强势"
        coef = 1.0
    elif m_basic >= 3:
        tier = "B档·过渡待确认"
        coef = 0.8
    elif m_basic >= 1:
        tier = "C档·偏弱"
        coef = 0.6
    else:
        tier = "D档·弱势"
        coef = 0.4

    return {
        "m_basic": m_basic,
        "m_basic_raw": round(m_basic_raw, 1),
        "same_direction_bonus": bonus,
        "tier": tier,
        "coefficient": coef,
        "index_details": all_scores,
    }


def calc_m_sector_from_df(sector_df: pd.DataFrame | None, lookback: int = 25) -> dict[str, Any]:
    """从行业ETF/指数DataFrame计算M_sector。"""
    if sector_df is None or len(sector_df) < 2:
        return {
            "m_sector_value": 7,
            "coefficient": 0.9,
            "tier": "跑平(默认)",
            "change_1d": 0.0,
            "change_5d": 0.0,
            "change_20d": 0.0,
            "momentum_score": 0.0,
        }

    closes = sector_df["close"].values.astype(float)
    n = len(closes)

    c1d = (closes[-1] - closes[-2]) / closes[-2] * 100 if n >= 2 else 0.0
    c5d = (closes[-1] - closes[-6]) / closes[-6] * 100 if n >= 6 else c1d
    c20d = (closes[-1] - closes[-21]) / closes[-21] * 100 if n >= 21 else c5d

    momentum = c1d * 0.3 + c5d * 0.4 + c20d * 0.3

    if momentum > 3:
        tier, coef, val = "强势领涨", 1.0, 10
    elif momentum > 1:
        tier, coef, val = "偏强", 0.95, 8
    elif momentum > -1:
        tier, coef, val = "跑平", 0.85, 6
    elif momentum > -3:
        tier, coef, val = "偏弱", 0.7, 4
    else:
        tier, coef, val = "弱势崩跌", 0.5, 1

    return {
        "m_sector_value": val,
        "coefficient": coef,
        "tier": tier,
        "change_1d": round(c1d, 2),
        "change_5d": round(c5d, 2),
        "change_20d": round(c20d, 2),
        "momentum_score": round(momentum, 2),
    }


def calc_m_factor(
    m_basic_result: dict[str, Any] | None = None,
    m_sector_result: dict[str, Any] | None = None,
) -> MFactorResult:
    """计算综合M因子。

    Args:
        m_basic_result: calc_m_basic_from_df() 的返回值；None 时使用默认中性。
        m_sector_result: calc_m_sector_from_df() 的返回值；None 时使用默认中性。
    """
    if m_basic_result is None:
        m_basic_result = {
            "m_basic": 2.0,
            "tier": "C档·偏弱(默认)",
            "coefficient": 0.6,
        }
    if m_sector_result is None:
        m_sector_result = {
            "m_sector_value": 7,
            "coefficient": 0.9,
            "tier": "跑平(默认)",
        }

    mb_coef = float(m_basic_result["coefficient"])
    ms_coef = float(m_sector_result["coefficient"])
    comprehensive = round(mb_coef * ms_coef * 10, 1)

    if comprehensive >= 4:
        action = "正常仓位"
        exposure = 0.80
    else:
        action = "触发收紧：仓位上限×60%，单只上限×15%"
        exposure = 0.48

    if m_basic_result.get("m_basic", 0) <= 0:
        action += " | D档弱势：总敞口≤30%"
        exposure = min(exposure, 0.30)

    return MFactorResult(
        m_basic=float(m_basic_result["m_basic"]),
        m_basic_tier=m_basic_result["tier"],
        m_basic_coefficient=mb_coef,
        m_sector_coefficient=ms_coef,
        m_sector_tier=m_sector_result["tier"],
        comprehensive_score=comprehensive,
        position_action=action,
        total_exposure_limit=exposure,
        details={
            "m_basic_raw": m_basic_result.get("m_basic_raw"),
            "same_direction_bonus": m_basic_result.get("same_direction_bonus"),
            "index_details": m_basic_result.get("index_details", {}),
            "sector_change_1d": m_sector_result.get("change_1d"),
            "sector_change_5d": m_sector_result.get("change_5d"),
            "sector_change_20d": m_sector_result.get("change_20d"),
            "sector_momentum": m_sector_result.get("momentum_score"),
        },
    )
