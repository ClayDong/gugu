"""市场状态判断模块：根据沪深300指数判断当前市场状态（牛市/熊市/震荡市）。"""
from __future__ import annotations

from datetime import date
from typing import Literal

import pandas as pd

from gugu.data import data_manager
from gugu.utils.log import get_logger

logger = get_logger()

# 沪深300指数代码
_HS300_SYMBOL = "000300"

# 均线周期
_MA_SHORT = 20
_MA_LONG = 60

# 仓位修正系数
_POSITION_MODIFIERS: dict[str, float] = {
    "bull": 1.0,
    "sideways": 0.5,
    "bear": 0.2,
}

Regime = Literal["bull", "bear", "sideways"]


class MarketRegimeDetector:
    """根据沪深300指数判断当前市场状态。

    判断逻辑：
    - MA20 > MA60 且 MA20 斜率 > 0 → 牛市（bull）
    - MA20 < MA60 且 MA20 斜率 < 0 → 熊市（bear）
    - 其他 → 震荡市（sideways）

    当日缓存：市场状态一天内不会剧变，同一天内重复调用直接返回缓存结果。
    """

    def __init__(self) -> None:
        self._cache_date: date | None = None
        self._cache_result: dict | None = None

    async def detect(self) -> dict:
        """检测当前市场状态。

        Returns:
            {
                "regime": "bull" | "bear" | "sideways",
                "trend_strength": float,  # 趋势强度 0-1
                "position_modifier": float,  # 仓位修正系数
                "reason": str,  # 判断理由
            }
        """
        # 当日缓存：同一天内直接返回
        today = date.today()
        if self._cache_date == today and self._cache_result is not None:
            return self._cache_result

        try:
            dm = data_manager()
            df = await dm.fetch_stock_history(_HS300_SYMBOL, days=_MA_LONG + 10)
            result = self._analyze(df)
        except Exception as e:
            logger.error(f"市场状态判断失败，安全降级为震荡市: {e}")
            result = {
                "regime": "sideways",
                "trend_strength": 0.0,
                "position_modifier": _POSITION_MODIFIERS["sideways"],
                "reason": f"数据获取失败，安全降级: {e}",
            }

        # 写入缓存
        self._cache_date = today
        self._cache_result = result
        return result

    def _analyze(self, df: pd.DataFrame) -> dict:
        """根据行情数据计算市场状态。"""
        if df is None or len(df) < _MA_LONG:
            return {
                "regime": "sideways",
                "trend_strength": 0.0,
                "position_modifier": _POSITION_MODIFIERS["sideways"],
                "reason": f"数据不足（需 {_MA_LONG} 行，实际 {len(df) if df is not None else 0} 行）",
            }

        close = df["close"]

        # 计算均线
        ma_short = close.rolling(window=_MA_SHORT).mean()
        ma_long = close.rolling(window=_MA_LONG).mean()

        # 取最新值
        ma20 = ma_short.iloc[-1]
        ma60 = ma_long.iloc[-1]

        # 计算 MA20 斜率：最近5日 MA20 的线性回归斜率方向
        recent_ma20 = ma_short.iloc[-5:]
        slope = self._calc_slope(recent_ma20)

        # 趋势强度 = |MA20 - MA60| / MA60
        trend_strength = abs(ma20 - ma60) / ma60 if ma60 != 0 else 0.0

        # 判断市场状态
        if ma20 > ma60 and slope > 0:
            regime: Regime = "bull"
            reason = f"MA20({ma20:.2f}) > MA60({ma60:.2f})，MA20斜率向上({slope:.4f})，牛市格局"
        elif ma20 < ma60 and slope < 0:
            regime = "bear"
            reason = f"MA20({ma20:.2f}) < MA60({ma60:.2f})，MA20斜率向下({slope:.4f})，熊市格局"
        else:
            regime = "sideways"
            if ma20 > ma60:
                reason = f"MA20({ma20:.2f}) > MA60({ma60:.2f})，但MA20斜率向下({slope:.4f})，震荡格局"
            elif ma20 < ma60:
                reason = f"MA20({ma20:.2f}) < MA60({ma60:.2f})，但MA20斜率向上({slope:.4f})，震荡格局"
            else:
                reason = f"MA20({ma20:.2f}) ≈ MA60({ma60:.2f})，震荡格局"

        return {
            "regime": regime,
            "trend_strength": round(trend_strength, 6),
            "position_modifier": _POSITION_MODIFIERS[regime],
            "reason": reason,
        }

    @staticmethod
    def _calc_slope(series: pd.Series) -> float:
        """计算序列的线性回归斜率（简单最小二乘法）。"""
        n = len(series)
        if n < 2:
            return 0.0
        # 去除 NaN
        valid = series.dropna()
        n = len(valid)
        if n < 2:
            return 0.0
        x = list(range(n))
        y = valid.values
        x_mean = sum(x) / n
        y_mean = sum(y) / n
        numerator = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
        denominator = sum((xi - x_mean) ** 2 for xi in x)
        if denominator == 0:
            return 0.0
        return numerator / denominator
