"""危险信号检测器：基于《炒股的智慧》的三大危险信号。

陈江挺的三大危险信号：
1. 量增价不涨 — 成交量放大但价格停滞，主力可能在出货
2. 两天转头 — 创新高后两天内价格转头向下，上升动能衰竭
3. 坏消息 — 突发利空导致跳空低开，市场情绪逆转

当危险信号出现时：
- 收紧移动止损至最近浪谷
- 不再加仓
- 准备随时离场
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


@dataclass
class DangerSignalResult:
    """危险信号检测结果。"""
    signals: list[str] = field(default_factory=list)  # 触发的危险信号列表
    severity: str = "none"  # none / low / medium / high
    description: str = ""   # 信号描述
    action: str = ""        # 建议操作

    @property
    def has_signal(self) -> bool:
        return len(self.signals) > 0


class DangerSignalDetector:
    """危险信号检测器。

    检测陈江挺提出的三大危险信号，以及补充的技术指标危险信号。

    检测逻辑：
    1. 量增价不涨：近5日成交量均量 > 20日均量 * 1.5，但价格涨幅 < 2%
    2. 两天转头：最近创20日新高后，2日内收盘价跌破新高日最低价
    3. 坏消息：当日跳空低开 > 3%，或单日跌幅 > 5%
    4. 量价背离（补充）：价格创新高但MACD/RSI未创新高
    5. 长上影线（补充）：上影线长度 > 实体的2倍，出现在高位
    """

    # 检测参数
    VOLUME_MA_SHORT = 5          # 短期成交量均线
    VOLUME_MA_LONG = 20          # 长期成交量均线
    VOLUME_SURGE_RATIO = 1.5     # 成交量放大倍数
    PRICE_STALL_THRESHOLD = 0.02 # 价格停滞阈值（2%）
    GAP_DOWN_THRESHOLD = -0.03   # 跳空低开阈值（-3%）
    BIG_DROP_THRESHOLD = -0.05   # 大跌阈值（-5%）
    HIGH_LOOKBACK = 20           # 新高回看周期
    SHADOW_BODY_RATIO = 2.0     # 上影线/实体倍数

    def detect(
        self,
        df: pd.DataFrame,
        prev_close: float | None = None,
    ) -> DangerSignalResult:
        """检测危险信号。

        Args:
            df: 行情DataFrame，需包含 close, high, low, volume 列
            prev_close: 昨收价（用于检测跳空低开）

        Returns:
            DangerSignalResult 检测结果
        """
        if df.empty or len(df) < self.VOLUME_MA_LONG + 5:
            return DangerSignalResult(
                severity="none",
                description="数据不足",
                action="观望",
            )

        signals: list[str] = []
        descriptions: list[str] = []

        try:
            # 1. 量增价不涨
            vol_stall = self._check_volume_price_stall(df)
            if vol_stall:
                signals.append("volume_price_stall")
                descriptions.append("量增价不涨：成交量放大但价格停滞，疑似主力出货")

            # 2. 两天转头
            reversal = self._check_two_day_reversal(df)
            if reversal:
                signals.append("two_day_reversal")
                descriptions.append("两天转头：创新高后两日内转头向下，上升动能衰竭")

            # 3. 坏消息（跳空低开/大跌）
            bad_news = self._check_bad_news(df, prev_close)
            if bad_news:
                signals.append("bad_news")
                descriptions.append("坏消息：跳空低开或单日大跌，市场情绪逆转")

            # 4. 量价背离
            divergence = self._check_price_volume_divergence(df)
            if divergence:
                signals.append("divergence")
                descriptions.append("量价背离：价格创新高但技术指标未创新高")

            # 5. 长上影线
            long_shadow = self._check_long_upper_shadow(df)
            if long_shadow:
                signals.append("long_upper_shadow")
                descriptions.append("长上影线：高位出现长上影，上方抛压沉重")

        except Exception as e:
            logger.warning(f"[danger-signal] 检测异常: {e}")
            return DangerSignalResult(
                severity="none",
                description=f"检测异常: {e}",
                action="观望",
            )

        # 评估严重程度
        severity = self._assess_severity(signals)

        result = DangerSignalResult(
            signals=signals,
            severity=severity,
            description="; ".join(descriptions) if descriptions else "无危险信号",
            action=self._suggest_action(severity, signals),
        )

        if signals:
            logger.warning(
                f"[danger-signal] 检测到 {len(signals)} 个危险信号: {signals}, "
                f"严重程度={severity}"
            )

        return result

    def _check_volume_price_stall(self, df: pd.DataFrame) -> bool:
        """检测量增价不涨。"""
        volume = df["volume"].astype(float) if "volume" in df.columns else None
        close = df["close"].astype(float)

        if volume is None or len(volume) < self.VOLUME_MA_LONG + self.VOLUME_MA_SHORT:
            return False

        vol_short = volume.iloc[-self.VOLUME_MA_SHORT:].mean()
        vol_long = volume.iloc[-self.VOLUME_MA_LONG:].mean()

        if vol_long <= 0:
            return False

        vol_ratio = vol_short / vol_long

        # 价格近5日涨幅
        price_change = (close.iloc[-1] - close.iloc[-self.VOLUME_MA_SHORT]) / close.iloc[-self.VOLUME_MA_SHORT]

        # 量增（>1.5倍）+ 价不涨（<2%）
        return vol_ratio > self.VOLUME_SURGE_RATIO and abs(price_change) < self.PRICE_STALL_THRESHOLD

    def _check_two_day_reversal(self, df: pd.DataFrame) -> bool:
        """检测两天转头。"""
        close = df["close"].astype(float)
        high = df["high"].astype(float) if "high" in df.columns else close
        low = df["low"].astype(float) if "low" in df.columns else close

        if len(close) < self.HIGH_LOOKBACK + 2:
            return False

        # 找最近20日的最高价位置
        recent_high_idx = close.iloc[-self.HIGH_LOOKBACK:].idxmax()
        high_pos = close.index.get_loc(recent_high_idx)

        # 最高价必须在最近5日之内（否则不是"近期"创新高）
        days_since_high = len(close) - 1 - high_pos
        if days_since_high > 3 or days_since_high < 1:
            return False

        # 检查创新高后2日内是否转头向下
        high_day_low = low.iloc[high_pos]
        for i in range(high_pos + 1, min(high_pos + 3, len(close))):
            if close.iloc[i] < high_day_low:
                return True

        return False

    def _check_bad_news(self, df: pd.DataFrame, prev_close: float | None) -> bool:
        """检测坏消息（跳空低开或大跌）。"""
        close = df["close"].astype(float)
        open_ = df["open"].astype(float) if "open" in df.columns else close

        if len(close) < 2:
            return False

        today_close = close.iloc[-1]
        today_open = open_.iloc[-1]

        # 使用传入的昨收价，或取df倒数第二日收盘
        ref_close = prev_close if prev_close and prev_close > 0 else close.iloc[-2]

        if ref_close <= 0:
            return False

        # 跳空低开 > 3%
        gap_pct = (today_open - ref_close) / ref_close
        if gap_pct < self.GAP_DOWN_THRESHOLD:
            return True

        # 单日跌幅 > 5%
        daily_return = (today_close - ref_close) / ref_close
        if daily_return < self.BIG_DROP_THRESHOLD:
            return True

        return False

    def _check_price_volume_divergence(self, df: pd.DataFrame) -> bool:
        """检测量价背离（简化版：价格新高但成交量未新高）。"""
        close = df["close"].astype(float)
        volume = df["volume"].astype(float) if "volume" in df.columns else None

        if volume is None or len(close) < self.HIGH_LOOKBACK:
            return False

        # 价格创新高
        current_price = close.iloc[-1]
        past_high = close.iloc[-self.HIGH_LOOKBACK:-1].max()

        if current_price <= past_high:
            return False  # 未创新高

        # 成交量未创新高
        current_vol = volume.iloc[-1]
        past_vol_max = volume.iloc[-self.HIGH_LOOKBACK:-1].max()

        return current_vol < past_vol_max * 0.8  # 成交量低于历史最高的80%

    def _check_long_upper_shadow(self, df: pd.DataFrame) -> bool:
        """检测高位长上影线。"""
        close = df["close"].astype(float)
        open_ = df["open"].astype(float) if "open" in df.columns else close
        high = df["high"].astype(float) if "high" in df.columns else close
        low = df["low"].astype(float) if "low" in df.columns else close

        if len(close) < self.HIGH_LOOKBACK:
            return False

        # 检查最近3日是否有长上影线
        for i in range(-3, 0):
            if abs(i) > len(close):
                continue

            body = abs(close.iloc[i] - open_.iloc[i])
            upper_shadow = high.iloc[i] - max(close.iloc[i], open_.iloc[i])

            if body <= 0:
                continue

            # 上影线 > 实体 * 2
            if upper_shadow > body * self.SHADOW_BODY_RATIO:
                # 且处于相对高位（近20日最高价附近）
                recent_high = close.iloc[-self.HIGH_LOOKBACK:].max()
                if close.iloc[i] > recent_high * 0.95:
                    return True

        return False

    @staticmethod
    def _assess_severity(signals: list[str]) -> str:
        """评估危险信号严重程度。"""
        if not signals:
            return "none"

        # 高严重度信号
        high_severity = {"bad_news", "two_day_reversal"}
        if any(s in high_severity for s in signals):
            return "high"

        # 多个信号同时出现
        if len(signals) >= 2:
            return "medium"

        return "low"

    @staticmethod
    def _suggest_action(severity: str, signals: list[str]) -> str:
        """根据严重程度建议操作。"""
        actions = {
            "none": "正常持有",
            "low": "保持警惕，收紧止损至最近浪谷",
            "medium": "准备离场，不再加仓，收紧止损",
            "high": "立即减仓/清仓，危险信号明确",
        }
        return actions.get(severity, "观望")
