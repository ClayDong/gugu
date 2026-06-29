"""多周期市场择时系统。

综合判断：
- 日线周期：MA20/MA60/MA120 趋势 + 成交量趋势
- 周线周期：MA5/MA10 周线级别大趋势
- 小时线周期：MA20/MA60 短期动能

输出：
- regime: "bull" | "bear" | "sideways" | "crash" | "rally"
- total_position_limit: 总仓位上限（0.0-1.0）
- buy_signal_allowed: 是否允许买入
- sell_signal_required: 是否强制卖出
- confidence: 判断置信度
- evidence: 各周期判断详情
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from gugu.data import data_manager
from gugu.utils.log import get_logger

logger = get_logger()

# 仓位映射表：根据市场状态决定总仓位上限
POSITION_LIMIT_MAP = {
    "bull": 0.80,      # 牛市：总仓位80%
    "rally": 0.60,     # 反弹：总仓位60%
    "sideways": 0.40,  # 震荡：总仓位40%
    "bear": 0.20,      # 熊市：总仓位20%
    "crash": 0.00,     # 暴跌：空仓
}


@dataclass
class RegimeEvidence:
    """单周期判断证据"""
    period: str                # daily / weekly / hourly
    signal: str                # bullish / bearish / neutral
    strength: float            # 信号强度 0-1
    details: dict[str, Any] = field(default_factory=dict)


class MultiPeriodRegimeDetector:
    """多周期市场择时器。

    核心逻辑（参考专业交易员的择时框架）：
    1. 周线定方向（大趋势）——权重40%
    2. 日线定状态（中期趋势）——权重40%
    3. 小时线定动能（短期动能）——权重20%

    每个周期综合判断：
    - 均线排列（MA排序）
    - 趋势强度（ADX概念）
    - 成交量验证
    """

    def __init__(self) -> None:
        self._dm = data_manager()
        self._cache: dict[str, Any] = {}
        self._cache_date: str = ""

    async def detect(self) -> dict[str, Any]:
        """检测当前市场状态并返回总仓位建议。"""
        from datetime import date
        today = str(date.today())
        if self._cache_date == today:
            return self._cache

        try:
            # 获取沪深300日线（250天）
            daily = await self._dm.fetch_stock_history("000300", days=250)
            if daily.empty or len(daily) < 120:
                return self._fallback()

            # 获取沪深300周线（通过日线resample）
            daily_indexed = daily.set_index("date")
            weekly = daily_indexed.resample("W").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum", "amount": "sum"
            }).dropna()

            # 各周期分析
            daily_evidence = self._analyze_period(daily, "daily",
                [("ma20", 20), ("ma60", 60), ("ma120", 120)])
            weekly_evidence = self._analyze_period(weekly.reset_index(), "weekly",
                [("ma5", 5), ("ma10", 10)])
            # Bug 修复：原代码用 daily.tail(30) 冒充小时线，语义错误。
            # 改为用日线短期均线(MA5/MA10)作为短期动能指标，正确标注为 short_term。
            short_term_evidence = self._analyze_period(daily.tail(30), "short_term",
                [("ma5", 5), ("ma10", 10)])

            # 综合评分
            score = (
                weekly_evidence.strength * 0.40 * (1 if weekly_evidence.signal == "bullish" else -1 if weekly_evidence.signal == "bearish" else 0) +
                daily_evidence.strength * 0.40 * (1 if daily_evidence.signal == "bullish" else -1 if daily_evidence.signal == "bearish" else 0) +
                short_term_evidence.strength * 0.20 * (1 if short_term_evidence.signal == "bullish" else -1 if short_term_evidence.signal == "bearish" else 0)
            )

            # 波动率判断（用于识别crash/rally）
            returns = daily["close"].pct_change().dropna()
            volatility = float(returns.std() * np.sqrt(252))  # 年化波动率
            recent_return = float(returns.tail(5).mean() * 252)  # 近5日年化收益率

            # 确定市场状态
            if recent_return < -0.50 and volatility > 0.40:
                regime = "crash"
            elif recent_return > 0.50 and volatility > 0.30:
                regime = "rally"
            elif score > 0.3:
                regime = "bull"
            elif score < -0.3:
                regime = "bear"
            else:
                regime = "sideways"

            total_limit = POSITION_LIMIT_MAP[regime]
            buy_allowed = regime not in ("crash",)
            sell_required = regime in ("bear", "crash")

            result = {
                "regime": regime,
                "total_position_limit": total_limit,
                "buy_signal_allowed": buy_allowed,
                "sell_signal_required": sell_required,
                "confidence": abs(score),
                "score": round(score, 3),
                "volatility": round(volatility, 3),
                "recent_return": round(recent_return, 3),
                "evidence": {
                    "daily": {"signal": daily_evidence.signal, "strength": daily_evidence.strength, "details": daily_evidence.details},
                    "weekly": {"signal": weekly_evidence.signal, "strength": weekly_evidence.strength, "details": weekly_evidence.details},
                    "short_term": {"signal": short_term_evidence.signal, "strength": short_term_evidence.strength, "details": short_term_evidence.details},
                },
                "reason": self._build_reason(regime, score, daily_evidence, weekly_evidence, short_term_evidence),
            }

            self._cache = result
            self._cache_date = today
            return result

        except Exception as e:
            logger.error(f"多周期择时检测失败: {e}")
            return self._fallback()

    def _analyze_period(self, df: pd.DataFrame, period: str,
                        ma_configs: list[tuple[str, int]]) -> RegimeEvidence:
        """分析单个周期的市场状态。"""
        close = df["close"].values
        if len(close) < max(m[1] for m in ma_configs):
            return RegimeEvidence(period=period, signal="neutral", strength=0.0)

        # 计算均线
        mas = {}
        for name, window in ma_configs:
            if len(close) >= window:
                mas[name] = pd.Series(close).rolling(window).mean().iloc[-1]

        # 均线排列判断
        ma_values = list(mas.values())
        if all(ma_values[i] > ma_values[i+1] for i in range(len(ma_values)-1)):
            alignment = "bullish"  # 多头排列
        elif all(ma_values[i] < ma_values[i+1] for i in range(len(ma_values)-1)):
            alignment = "bearish"  # 空头排列
        else:
            alignment = "neutral"  # 交织

        # 趋势强度（价格相对MA的偏离度）
        if mas:
            price = close[-1]
            ma_ref = list(mas.values())[0]  # 最短周期均线
            if ma_ref > 0:
                deviation = abs(price - ma_ref) / ma_ref
                strength = min(deviation * 5, 1.0)  # 归一化到0-1
            else:
                strength = 0.0
        else:
            strength = 0.0

        # 成交量验证
        if "volume" in df.columns:
            vol_ma20 = df["volume"].rolling(20).mean().iloc[-1] if len(df) >= 20 else df["volume"].mean()
            vol_today = df["volume"].iloc[-1]
            vol_confirm = vol_today > vol_ma20 * 1.2  # 放量20%以上
        else:
            vol_confirm = False

        details = {
            "mas": {k: round(v, 2) for k, v in mas.items()},
            "price": round(float(close[-1]), 2),
            "alignment": alignment,
            "vol_confirm": vol_confirm,
        }

        return RegimeEvidence(
            period=period, signal=alignment, strength=strength, details=details
        )

    def _build_reason(self, regime: str, score: float,
                      daily: RegimeEvidence, weekly: RegimeEvidence,
                      short_term: RegimeEvidence) -> str:
        """构建可读的判断理由。"""
        regime_cn = {"bull": "牛市", "bear": "熊市", "sideways": "震荡",
                     "crash": "暴跌", "rally": "反弹"}
        period_cn = {"daily": "日线", "weekly": "周线", "short_term": "短期"}
        signal_cn = {"bullish": "多头", "bearish": "空头", "neutral": "中性"}

        parts = [f"综合评分{score:.2f}，判定为{regime_cn.get(regime, regime)}"]
        for ev in [weekly, daily, short_term]:
            parts.append(f"{period_cn.get(ev.period, ev.period)}{signal_cn.get(ev.signal, ev.signal)}(强度{ev.strength:.2f})")
        return "；".join(parts)

    def _fallback(self) -> dict[str, Any]:
        """安全降级：数据获取失败时返回保守设置。"""
        return {
            "regime": "sideways",
            "total_position_limit": 0.40,
            "buy_signal_allowed": True,
            "sell_signal_required": False,
            "confidence": 0.0,
            "score": 0.0,
            "volatility": 0.0,
            "recent_return": 0.0,
            "evidence": {},
            "reason": "数据获取失败，降级为保守模式",
        }