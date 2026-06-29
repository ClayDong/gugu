"""四阶段判断器：基于《炒股的智慧》的股票走势阶段识别。

陈江挺将股票走势分为四个阶段：
1. 牛皮市（Sideways）- 低位盘整，波动小，无明确趋势
2. 正常升势（Normal Uptrend）- 更高的高点和低点，温和放量
3. 疯狂（Frenzy）- 陡峭上升，成交量激增，市场关注度高
4. 最后（Final/Climax）- 抛物线走势，衰竭缺口，极端成交量

每个阶段需要不同的操作策略：
- 牛皮市：观望为主，可小仓位试探突破方向
- 正常升势：顺势加仓，分层下注，设好止损
- 疯狂：谨慎持有，收紧止损，不追高
- 最后：准备离场，出现危险信号立即卖出
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from gugu.utils.log import get_logger

logger = get_logger()


class MarketStage(str, Enum):
    """股票走势四阶段。"""
    SIDEWAYS = "sideways"          # 牛皮市 - 盘整
    NORMAL_UPTREND = "normal_up"   # 正常升势
    FRENZY = "frenzy"              # 疯狂
    FINAL = "final"                # 最后阶段
    DOWNTREND = "downtrend"        # 下降趋势（补充）


@dataclass
class StageResult:
    """阶段判断结果。"""
    stage: MarketStage
    confidence: float          # 判断置信度 0-1
    description: str           # 阶段描述
    suggestion: str            # 操作建议
    metrics: dict[str, Any]    # 判断指标详情


class StageDetector:
    """四阶段判断器。

    基于波浪结构、量价关系、波动率综合判断股票当前所处的阶段。

    判断逻辑：
    1. 计算近期高低点序列，判断波浪结构（更高高点/低点 = 上升趋势）
    2. 计算移动平均线斜率，判断趋势强度
    3. 计算成交量变化率，判断资金参与度
    4. 计算ATR比率（波动率），判断市场状态
    5. 综合以上指标映射到四阶段
    """

    # 阶段判断参数
    LOOKBACK_PERIOD = 60           # 回看周期（约3个月）
    WAVE_WINDOW = 5                # 波浪高低点识别窗口
    MA_SHORT = 10                  # 短期均线
    MA_MEDIUM = 20                 # 中期均线
    MA_LONG = 60                   # 长期均线
    VOLUME_MA = 20                 # 成交量均线
    FRENZY_VOLUME_RATIO = 2.0      # 疯狂阶段成交量倍数
    FRENZY_SLOPE_THRESHOLD = 0.03  # 疯狂阶段MA斜率阈值
    FINAL_SLOPE_THRESHOLD = 0.05   # 最后阶段MA斜率阈值
    SIDEWAYS_VOLATILITY_MAX = 0.02 # 牛皮市最大波动率

    def detect(self, df: pd.DataFrame) -> StageResult:
        """判断股票当前所处的阶段。

        Args:
            df: 行情DataFrame，需包含 close, high, low, volume 列

        Returns:
            StageResult 判断结果
        """
        if df.empty or len(df) < self.MA_LONG + 10:
            return StageResult(
                stage=MarketStage.SIDEWAYS,
                confidence=0.0,
                description="数据不足，无法判断阶段",
                suggestion="观望",
                metrics={},
            )

        try:
            metrics = self._calculate_metrics(df)
        except Exception as e:
            logger.warning(f"[stage-detector] 指标计算失败: {e}")
            return StageResult(
                stage=MarketStage.SIDEWAYS,
                confidence=0.0,
                description=f"指标计算异常: {e}",
                suggestion="观望",
                metrics={},
            )

        stage, confidence = self._classify_stage(metrics)
        description = self._stage_description(stage, metrics)
        suggestion = self._stage_suggestion(stage)

        logger.info(
            f"[stage-detector] 阶段={stage.value}, 置信度={confidence:.2f}, "
            f"斜率={metrics.get('ma_slope', 0):.4f}, 量比={metrics.get('volume_ratio', 1):.2f}"
        )

        return StageResult(
            stage=stage,
            confidence=confidence,
            description=description,
            suggestion=suggestion,
            metrics=metrics,
        )

    def _calculate_metrics(self, df: pd.DataFrame) -> dict[str, Any]:
        """计算阶段判断所需的各项指标。"""
        close = df["close"].astype(float)
        high = df["high"].astype(float) if "high" in df.columns else close
        low = df["low"].astype(float) if "low" in df.columns else close
        volume = df["volume"].astype(float) if "volume" in df.columns else pd.Series([1] * len(df))

        # 取最近LOOKBACK_PERIOD根K线
        lookback = min(self.LOOKBACK_PERIOD, len(df))
        close_recent = close.iloc[-lookback:]
        high_recent = high.iloc[-lookback:]
        low_recent = low.iloc[-lookback:]
        vol_recent = volume.iloc[-lookback:]

        # 1. 移动平均线
        ma_short = close.rolling(self.MA_SHORT).mean().iloc[-1]
        ma_medium = close.rolling(self.MA_MEDIUM).mean().iloc[-1]
        ma_long = close.rolling(self.MA_LONG).mean().iloc[-1]

        # 2. MA斜率（中期均线20日变化率）
        ma_medium_prev = close.rolling(self.MA_MEDIUM).mean().iloc[-self.MA_MEDIUM - 1]
        ma_slope = (ma_medium - ma_medium_prev) / ma_medium_prev if ma_medium_prev > 0 else 0

        # 3. 波浪结构：识别更高高点/更高低点
        wave_highs = self._find_swings(high_recent, self.WAVE_WINDOW, high=True)
        wave_lows = self._find_swings(low_recent, self.WAVE_WINDOW, high=False)

        higher_highs = self._is_ascending(wave_highs)
        higher_lows = self._is_ascending(wave_lows)

        # 4. 成交量变化
        vol_ma = vol_recent.rolling(self.VOLUME_MA).mean().iloc[-1]
        vol_recent_avg = vol_recent.iloc[-5:].mean()
        volume_ratio = vol_recent_avg / vol_ma if vol_ma > 0 else 1.0

        # 5. 波动率（ATR比率）
        atr = self._calculate_atr(high, low, close, 14)
        atr_ratio = atr / close.iloc[-1] if close.iloc[-1] > 0 else 0

        # 6. 近期涨幅
        price_change_20 = (close.iloc[-1] - close.iloc[-min(21, len(close))]) / close.iloc[-min(21, len(close))]

        # 7. 均线排列
        ma_bullish = ma_short > ma_medium > ma_long
        ma_bearish = ma_short < ma_medium < ma_long

        return {
            "ma_short": float(ma_short),
            "ma_medium": float(ma_medium),
            "ma_long": float(ma_long),
            "ma_slope": float(ma_slope),
            "higher_highs": higher_highs,
            "higher_lows": higher_lows,
            "wave_highs": [float(x) for x in wave_highs[-3:]],
            "wave_lows": [float(x) for x in wave_lows[-3:]],
            "volume_ratio": float(volume_ratio),
            "atr_ratio": float(atr_ratio),
            "price_change_20": float(price_change_20),
            "ma_bullish": bool(ma_bullish),
            "ma_bearish": bool(ma_bearish),
            "current_price": float(close.iloc[-1]),
        }

    @staticmethod
    def _find_swings(series: pd.Series, window: int, high: bool = True) -> list[float]:
        """识别波浪的高点或低点。"""
        swings: list[float] = []
        for i in range(window, len(series) - window):
            segment = series.iloc[i - window : i + window + 1]
            if high:
                if series.iloc[i] == segment.max():
                    swings.append(float(series.iloc[i]))
            else:
                if series.iloc[i] == segment.min():
                    swings.append(float(series.iloc[i]))
        # 末尾点
        if len(series) > 0:
            swings.append(float(series.iloc[-1]))
        return swings

    @staticmethod
    def _is_ascending(values: list[float]) -> bool:
        """判断序列是否递增（至少最后2个点递增）。"""
        if len(values) < 2:
            return False
        return values[-1] > values[-2]

    @staticmethod
    def _calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> float:
        """计算ATR（平均真实波幅）。"""
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        return float(atr) if not np.isnan(atr) else 0.0

    def _classify_stage(self, metrics: dict[str, Any]) -> tuple[MarketStage, float]:
        """根据指标分类阶段。

        判断优先级：
        1. 最后阶段（极端斜率+极端量比）
        2. 疯狂阶段（高斜率+高量比）
        3. 正常升势（正向斜率+更高高低点）
        4. 牛皮市（低波动+无趋势）
        5. 下降趋势（负向斜率+更低高低点）
        """
        ma_slope = metrics.get("ma_slope", 0)
        volume_ratio = metrics.get("volume_ratio", 1)
        atr_ratio = metrics.get("atr_ratio", 0)
        higher_highs = metrics.get("higher_highs", False)
        higher_lows = metrics.get("higher_lows", False)
        ma_bullish = metrics.get("ma_bullish", False)
        ma_bearish = metrics.get("ma_bearish", False)
        price_change_20 = metrics.get("price_change_20", 0)

        # 最后阶段：抛物线走势
        if ma_slope > self.FINAL_SLOPE_THRESHOLD and volume_ratio > self.FRENZY_VOLUME_RATIO:
            confidence = min(0.9, abs(ma_slope) / self.FINAL_SLOPE_THRESHOLD * 0.5 + 0.4)
            return MarketStage.FINAL, confidence

        # 疯狂阶段：陡峭上升+成交量放大
        if ma_slope > self.FRENZY_SLOPE_THRESHOLD and volume_ratio > 1.5:
            confidence = min(0.85, abs(ma_slope) / self.FRENZY_SLOPE_THRESHOLD * 0.5 + 0.35)
            return MarketStage.FRENZY, confidence

        # 正常升势：均线多头排列+更高高低点
        if ma_bullish and higher_highs and higher_lows and ma_slope > 0:
            confidence = min(0.85, 0.5 + abs(ma_slope) * 10)
            return MarketStage.NORMAL_UPTREND, confidence

        # 弱正常升势：有上升趋势但不够强
        if higher_highs and higher_lows and ma_slope > 0.005:
            confidence = 0.55
            return MarketStage.NORMAL_UPTREND, confidence

        # 下降趋势
        if ma_bearish and not higher_highs and not higher_lows and ma_slope < 0:
            confidence = min(0.85, 0.5 + abs(ma_slope) * 10)
            return MarketStage.DOWNTREND, confidence

        # 牛皮市：低波动+无明确趋势
        if atr_ratio < self.SIDEWAYS_VOLATILITY_MAX and abs(ma_slope) < 0.01:
            confidence = 0.6
            return MarketStage.SIDEWAYS, confidence

        # 默认：根据斜率方向判断
        if ma_slope > 0:
            return MarketStage.NORMAL_UPTREND, 0.4
        elif ma_slope < 0:
            return MarketStage.DOWNTREND, 0.4
        else:
            return MarketStage.SIDEWAYS, 0.3

    @staticmethod
    def _stage_description(stage: MarketStage, metrics: dict[str, Any]) -> str:
        """生成阶段描述。"""
        descriptions = {
            MarketStage.SIDEWAYS: (
                f"牛皮市盘整阶段。波动率 {metrics.get('atr_ratio', 0):.2%}，"
                f"MA斜率 {metrics.get('ma_slope', 0):.4f}。"
                f"股价在窄幅区间内震荡，无明确趋势方向。"
            ),
            MarketStage.NORMAL_UPTREND: (
                f"正常升势阶段。MA斜率 {metrics.get('ma_slope', 0):.4f}，"
                f"成交量比 {metrics.get('volume_ratio', 1):.2f}。"
                f"更高高点和更高低点排列，均线多头排列。"
            ),
            MarketStage.FRENZY: (
                f"疯狂上涨阶段。MA斜率 {metrics.get('ma_slope', 0):.4f}，"
                f"成交量比 {metrics.get('volume_ratio', 1):.2f}。"
                f"股价陡峭上升，成交量显著放大，市场情绪亢奋。"
            ),
            MarketStage.FINAL: (
                f"最后阶段/见顶信号。MA斜率 {metrics.get('ma_slope', 0):.4f}，"
                f"成交量比 {metrics.get('volume_ratio', 1):.2f}。"
                f"抛物线走势，成交量极端放大，可能接近顶部。"
            ),
            MarketStage.DOWNTREND: (
                f"下降趋势阶段。MA斜率 {metrics.get('ma_slope', 0):.4f}。"
                f"更低高点和更低低点排列，均线空头排列。"
            ),
        }
        return descriptions.get(stage, "未知阶段")

    @staticmethod
    def _stage_suggestion(stage: MarketStage) -> str:
        """生成操作建议。"""
        suggestions = {
            MarketStage.SIDEWAYS: "观望为主，可小仓位试探突破方向，设好止损",
            MarketStage.NORMAL_UPTREND: "顺势持有/加仓，分层下注，用移动止损保护利润",
            MarketStage.FRENZY: "谨慎持有，收紧止损至最近浪谷，不追高加仓",
            MarketStage.FINAL: "准备离场，出现危险信号立即卖出，不再加仓",
            MarketStage.DOWNTREND: "空仓观望，不抄底，等待趋势反转信号",
        }
        return suggestions.get(stage, "观望")
