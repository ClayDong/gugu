"""MACD 零轴上方回调策略（macd_zero_axis）。

来源：用户提供的六句 MACD 口诀，落地为可证伪的 paper 观察策略。

买入：DIF/DEA 都在零轴上方 + 金叉后出现 MA20/近5日低点回踩确认。
卖出：趋势破坏、顶背离证据或移动止损。顶背离先记录为 evidence，默认全平，
不在信号层伪造“减仓 50%”成交。

注意：这是实验策略，不能据此宣称盈利；必须经过统一 GatePolicy。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()

__all__ = ["MacdZeroAxisStrategy"]


class MacdZeroAxisStrategy(Strategy):
    """零轴上方 MACD 金叉回调确认策略。"""

    name = "macd_zero_axis"
    category = "trend"

    def validate_params(self) -> None:
        fast = int(self.params.get("macd_fast", 12))
        slow = int(self.params.get("macd_slow", 26))
        signal = int(self.params.get("macd_signal", 9))
        if not 0 < fast < slow or signal <= 0:
            raise ValueError("MACD 参数必须满足 fast < slow 且 signal > 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        fast = int(self.params.get("macd_fast", 12))
        slow = int(self.params.get("macd_slow", 26))
        signal_window = int(self.params.get("macd_signal", 9))
        confirm_window = int(self.params.get("confirm_window", 5))
        pullback_tolerance = float(self.params.get("pullback_tolerance", 0.03))
        trailing_stop_pct = float(self.params.get("trailing_stop_pct", 0.08))

        ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
        df["dif"] = ema_fast - ema_slow
        df["dea"] = df["dif"].ewm(span=signal_window, adjust=False).mean()
        df["ma20"] = df["close"].rolling(20).mean()
        df["low5_prev"] = df["low"].rolling(5).min().shift(1)

        zero_axis = (df["dif"] > 0) & (df["dea"] > 0)
        golden_cross = (df["dif"] > df["dea"]) & (df["dif"].shift(1) <= df["dea"].shift(1))
        cross_recent = golden_cross.rolling(confirm_window, min_periods=1).max().astype(bool)

        # 回踩确认：靠近 MA20，或近 5 日低点未破；必须仍在零轴上方。
        near_ma20 = (df["close"] - df["ma20"]).abs() / df["ma20"].replace(
            0, np.nan
        ) <= pullback_tolerance
        low_holds = df["low"] >= df["low5_prev"] * (1 - pullback_tolerance)
        pullback_confirmed = near_ma20 | low_holds
        buy_cond = zero_axis & cross_recent & pullback_confirmed & df["ma20"].notna()

        # 顶背离证据：价格创新 20 日高，但 DIF 没有创新高。
        price_high = df["close"] >= df["close"].rolling(20).max()
        dif_high = df["dif"] >= df["dif"].rolling(20).max()
        top_divergence = price_high & ~dif_high & (df["dif"] > 0)
        df["macd_top_divergence"] = top_divergence

        trend_break = (df["close"] < df["ma20"]) | (df["close"] < df["low5_prev"])
        sell_raw = trend_break | top_divergence

        # 持仓感知卖出，避免无持仓刷卖出信号。
        buy_arr = buy_cond.to_numpy(dtype=bool)
        sell_arr = sell_raw.to_numpy(dtype=bool)
        in_position = False
        valid_sell = np.zeros(len(df), dtype=bool)
        for i in range(len(df)):
            if buy_arr[i]:
                in_position = True
            if sell_arr[i] and in_position:
                valid_sell[i] = True
                in_position = False

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[sell_raw & pd.Series(valid_sell, index=df.index), "signal"] = -1

        dif_gap = ((df["dif"] - df["dea"]) / df["close"].replace(0, np.nan)).clip(0, 0.05)
        df["confidence"] = 0.0
        df.loc[buy_cond, "confidence"] = (dif_gap / 0.05).clip(0.3, 1.0)
        df.loc[sell_raw & pd.Series(valid_sell, index=df.index), "confidence"] = 0.6

        if trailing_stop_pct > 0:
            df = self._apply_trailing_stop_exit(df, trailing_stop_pct)

        logger.info(
            "macd_zero_axis: {} 买入, {} 卖出, 顶背离证据 {}",
            int((df["signal"] == 1).sum()),
            int((df["signal"] == -1).sum()),
            int(top_divergence.sum()),
        )
        return df
