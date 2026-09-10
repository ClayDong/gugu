"""斌哥趋势极简战法。

来源：斌哥Bg视频（2026-08-16）转写文档。

核心：EMA10/EMA20/EMA30三线组合 + 格兰威尔八大买卖点。

买入信号（格兰威尔买入点）：
1. 突破：价格突破均线（三线多头排列后首次回调买入）
2. 回踩：价格回调到EMA20附近获支撑 + 放量阳线

卖出信号：
1. 跌破EMA20（生命线）
2. 三线空头排列
3. 移动止损8%
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()

__all__ = ["BingeTrendStrategy"]


class BingeTrendStrategy(Strategy):
    """斌哥趋势极简战法。"""

    name = "binge_trend"
    category = "trend"

    def validate_params(self) -> None:
        if int(self.params.get("ema_short", 10)) <= 0:
            raise ValueError("ema_short 必须 > 0")
        if int(self.params.get("ema_mid", 20)) <= int(self.params.get("ema_short", 10)):
            raise ValueError("ema_mid 必须大于 ema_short")
        if int(self.params.get("ema_long", 30)) <= int(self.params.get("ema_mid", 20)):
            raise ValueError("ema_long 必须大于 ema_mid")
        if float(self.params.get("trailing_stop_pct", 0.08)) <= 0:
            raise ValueError("trailing_stop_pct 必须 > 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        ema_short = int(self.params.get("ema_short", 10))
        ema_mid = int(self.params.get("ema_mid", 20))
        ema_long = int(self.params.get("ema_long", 30))
        pullback_tolerance = float(self.params.get("pullback_tolerance", 0.03))
        vol_ratio_min = float(self.params.get("vol_ratio_min", 1.2))
        trailing_stop_pct = float(self.params.get("trailing_stop_pct", 0.08))

        df["ema_short"] = df["close"].ewm(span=ema_short, adjust=False).mean()
        df["ema_mid"] = df["close"].ewm(span=ema_mid, adjust=False).mean()
        df["ema_long"] = df["close"].ewm(span=ema_long, adjust=False).mean()
        df["vol_ma20"] = df["volume"].rolling(ema_mid).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma20"].replace(0, np.nan)
        df["is_yang"] = df["close"] > df["open"]

        # 三线多头排列
        bullish_alignment = (df["ema_short"] > df["ema_mid"]) & (df["ema_mid"] > df["ema_long"])
        # 三线空头排列
        bearish_alignment = (df["ema_short"] < df["ema_mid"]) & (df["ema_mid"] < df["ema_long"])

        # 回踩EMA20获支撑
        near_ema_mid = (df["close"] - df["ema_mid"]).abs() / df["ema_mid"].replace(
            0, np.nan
        ) <= pullback_tolerance
        above_ema_mid = df["close"] > df["ema_mid"]

        # 买入：多头排列 + 回踩EMA20 + 放量阳线
        buy_cond = (
            bullish_alignment
            & near_ema_mid
            & above_ema_mid
            & df["is_yang"]
            & (df["vol_ratio"] >= vol_ratio_min)
        ).fillna(False)

        # 卖出：跌破EMA20 或 空头排列
        sell_raw = (df["close"] < df["ema_mid"]) | bearish_alignment
        buy_arr = buy_cond.to_numpy(dtype=bool)
        sell_arr = sell_raw.fillna(False).to_numpy(dtype=bool)
        in_position = False
        valid_sell = np.zeros(len(df), dtype=bool)
        for i in range(len(df)):
            if buy_arr[i]:
                in_position = True
            elif sell_arr[i] and in_position:
                valid_sell[i] = True
                in_position = False

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[pd.Series(valid_sell, index=df.index), "signal"] = -1

        # 置信度：三线排列强度 + 量能
        alignment_strength = (
            (df["ema_short"] - df["ema_long"]) / df["ema_long"].replace(0, np.nan)
        ).clip(0, 0.1) / 0.1
        vol_strength = ((df["vol_ratio"] - 1) / 1.5).clip(0, 1)
        df["confidence"] = 0.0
        df.loc[buy_cond, "confidence"] = (
            (alignment_strength * 0.4 + vol_strength * 0.3 + 0.3).clip(0, 1).loc[buy_cond]
        )

        if trailing_stop_pct > 0:
            df = self._apply_trailing_stop_exit(df, trailing_stop_pct)

        buy_count = int((df["signal"] == 1).sum())
        sell_count = int((df["signal"] == -1).sum())
        logger.info(
            "binge_trend 信号统计: {} 买入, {} 卖出",
            buy_count,
            sell_count,
        )
        return df
