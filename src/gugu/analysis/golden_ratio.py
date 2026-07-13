"""Golden-Ratio五档位引擎（V5框架集成）。

基于枢轴点（Pivot）自动波段识别，计算斐波那契回撤五档位，
实现军规0.5线自动折扣机制。

核心逻辑（来自V5.6投资框架，回测验证）：
- 按市值选择分析窗口（>1000亿→120日，200-1000亿→90日，<200亿→60日）
- 在窗口内自动识别Swing High/Swing Low枢轴点
- 取最近一波完整推动浪计算斐波那契档位：0.191/0.382/0.5(军规)/0.618/0.786
- 当前价格在军规线(0.5)下方 → 触发军规折扣（信号置信度×0.9）

五档位区域划分：
  ≤0.191  强势区(0-19.1%)     → 正常持有/可轻仓追涨
  0.191-0.382 黄金建仓区       → 关注回调企稳，可建仓
  0.382-0.500 警戒区           → 缩紧止损，谨慎操作
  0.500-0.618 军规区(破0.5)    → 军规已破！禁止新建仓
  0.618-0.786 深调区           → 趋势可能结束，等待企稳
  >0.786  超跌区               → 深度回调，可能超跌反弹
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()

FIB_LEVELS = [0.191, 0.382, 0.500, 0.618, 0.786]


@dataclass
class FibonacciLevel:
    ratio: float
    price: float
    label: str


@dataclass
class GoldenRatioResult:
    current_price: float
    trend_direction: str
    anchor_high: float
    anchor_low: float
    anchor_high_date: str
    anchor_low_date: str
    window_days: int
    wave_amplitude_pct: float
    levels: list[FibonacciLevel]
    current_callback_pct: float
    military_rule_broken: bool
    action_zone: str
    suggestion: str
    position_desc: str
    discount_factor: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_price": round(self.current_price, 2),
            "trend_direction": self.trend_direction,
            "anchor_high": round(self.anchor_high, 2),
            "anchor_low": round(self.anchor_low, 2),
            "anchor_high_date": self.anchor_high_date,
            "anchor_low_date": self.anchor_low_date,
            "window_days": self.window_days,
            "wave_amplitude_pct": round(self.wave_amplitude_pct, 1),
            "levels": [
                {"ratio": lv.ratio, "price": round(lv.price, 2), "label": lv.label}
                for lv in self.levels
            ],
            "current_callback_pct": round(self.current_callback_pct * 100, 1),
            "military_rule_broken": self.military_rule_broken,
            "action_zone": self.action_zone,
            "suggestion": self.suggestion,
            "position_desc": self.position_desc,
            "discount_factor": self.discount_factor,
        }


def _select_window_by_marketcap(market_cap: float | None) -> int:
    if market_cap is None:
        return 90
    if market_cap > 1000:
        return 120
    if market_cap > 200:
        return 90
    return 60


def _find_pivots(
    highs: np.ndarray, lows: np.ndarray, left: int = 5, right: int = 5
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """枢轴点识别（Swing High/Low）。

    一个点是Swing High需要：left根K线最高价都<它，且right根K线最高价都<它。
    Swing Low同理。
    """
    n = len(highs)
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []

    for i in range(left, n - right):
        h = highs[i]
        lo = lows[i]

        is_high = all(highs[i - j] < h for j in range(1, left + 1)) and all(
            highs[i + j] < h for j in range(1, right + 1)
        )
        is_low = all(lows[i - j] > lo for j in range(1, left + 1)) and all(
            lows[i + j] > lo for j in range(1, right + 1)
        )

        if is_high:
            swing_highs.append((i, float(h)))
        if is_low:
            swing_lows.append((i, float(lo)))

    return swing_highs, swing_lows


def _calc_fib_levels(high: float, low: float) -> list[FibonacciLevel]:
    diff = high - low
    labels = {
        0.191: "0.191-强势线",
        0.382: "0.382-黄金线",
        0.500: "0.500-军规线",
        0.618: "0.618-深调线",
        0.786: "0.786-超跌线",
    }
    return [FibonacciLevel(ratio=r, price=high - diff * r, label=labels[r]) for r in FIB_LEVELS]


def calc_golden_ratio(
    df: pd.DataFrame, market_cap: float | None = None
) -> GoldenRatioResult | None:
    """计算Golden-Ratio五档位。

    Args:
        df: 日K线DataFrame，需包含 open/high/low/close/volume 列，索引为日期。
        market_cap: 总市值（亿元），用于选择窗口。None 则使用默认90日。

    Returns:
        GoldenRatioResult 或 None（数据不足时）。
    """
    if df is None or len(df) < 30:
        return None

    window = _select_window_by_marketcap(market_cap)
    if len(df) < window:
        window = len(df)

    recent = df.iloc[-window:].copy()
    highs = recent["high"].values.astype(float)
    lows = recent["low"].values.astype(float)
    closes = recent["close"].values.astype(float)
    dates = (
        recent.index.strftime("%Y-%m-%d").tolist()
        if hasattr(recent.index, "strftime")
        else [str(d) for d in recent.index]
    )
    current_price = float(closes[-1])

    pivot_left = max(3, min(8, window // 15))
    swing_highs, swing_lows = _find_pivots(highs, lows, left=pivot_left, right=pivot_left)

    trend = "震荡"
    ah_idx = al_idx = -1

    if swing_highs and swing_lows:
        last_high_idx, last_high_price = swing_highs[-1]
        last_low_idx, last_low_price = swing_lows[-1]

        if last_high_idx > last_low_idx:
            prev_lows = [li for li, _ in swing_lows if li < last_high_idx]
            swing_low_idx = prev_lows[-1] if prev_lows else last_low_idx
            swing_low_price = float(lows[swing_low_idx])
            ah_idx, ah = last_high_idx, last_high_price
            al_idx, al = swing_low_idx, swing_low_price
            trend = "上涨后回调"
        else:
            prev_highs = [hi for hi, _ in swing_highs if hi < last_low_idx]
            swing_high_idx = prev_highs[-1] if prev_highs else last_high_idx
            swing_high_price = float(highs[swing_high_idx])
            ah_idx, ah = swing_high_idx, swing_high_price
            al_idx, al = last_low_idx, last_low_price
            trend = "下跌后反弹"
    else:
        ah = float(highs.max())
        al = float(lows.min())
        ah_idx = int(highs.argmax())
        al_idx = int(lows.argmin())

    if ah <= al or (ah - al) / max(al, 0.01) < 0.03:
        ah = float(highs.max())
        al = float(lows.min())
        ah_idx = int(highs.argmax())
        al_idx = int(lows.argmin())
        trend = "震荡"

    ahd = dates[ah_idx] if 0 <= ah_idx < len(dates) else ""
    ald = dates[al_idx] if 0 <= al_idx < len(dates) else ""

    levels = _calc_fib_levels(ah, al)
    diff = ah - al

    if trend in ("上涨后回调", "震荡"):
        cb = (ah - current_price) / diff if diff > 0 else 0
    else:
        raw = (current_price - al) / diff if diff > 0 else 0
        cb = 1 - raw

    cb = max(0.0, min(1.0, cb))

    mb = cb > 0.5
    discount = 0.9 if mb else 1.0

    if cb <= 0.191:
        az, sug = "强势区(0-19.1%)", "正常持有/可轻仓追涨"
    elif cb <= 0.382:
        az, sug = "黄金建仓区(19.1-38.2%)", "关注回调企稳，可建仓"
    elif cb <= 0.5:
        az, sug = "警戒区(38.2-50%)", "缩紧止损，谨慎操作"
    elif cb <= 0.618:
        az, sug = "军规区(50-61.8%)", "军规已破！禁止新建仓"
    elif cb <= 0.786:
        az, sug = "深调区(61.8-78.6%)", "趋势可能结束，等待企稳"
    else:
        az, sug = "超跌区(>78.6%)", "深度回调，可能超跌反弹"

    if mb:
        sug += " | 军规折扣:×0.9"

    if current_price >= ah:
        position_desc = "突破波段高点，强势"
    elif current_price >= levels[0].price:
        position_desc = "0.191上方，强势区"
    elif current_price >= levels[1].price:
        position_desc = "0.191-0.382黄金区"
    elif current_price >= levels[2].price:
        position_desc = "0.382-0.500弱黄金区"
    elif current_price >= levels[3].price:
        position_desc = "0.500军规-0.618"
    elif current_price >= levels[4].price:
        position_desc = "0.618-0.786深调"
    elif current_price >= al:
        position_desc = "0.786下方，近锚点低"
    else:
        position_desc = "跌破波段低点"

    wave_amplitude = (ah - al) / max(al, 0.01) * 100

    return GoldenRatioResult(
        current_price=current_price,
        trend_direction=trend,
        anchor_high=ah,
        anchor_low=al,
        anchor_high_date=ahd,
        anchor_low_date=ald,
        window_days=window,
        wave_amplitude_pct=wave_amplitude,
        levels=levels,
        current_callback_pct=cb,
        military_rule_broken=mb,
        action_zone=az,
        suggestion=sug,
        position_desc=position_desc,
        discount_factor=discount,
    )
