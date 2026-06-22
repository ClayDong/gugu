"""突破策略：达尔文箱体、Dual Thrust、支撑阻力。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class BoxBreakoutStrategy(Strategy):
    """达尔文箱体理论：箱体震荡突破为信号。

    识别近期价格箱体（最高/最低），突破箱体上沿买入，跌破下沿卖出。
    可选成交量确认。
    """

    name = "box_breakout"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        w = int(self.params["box_window"])
        vol_confirm = bool(self.params.get("volume_confirm", True))
        vol_ratio = float(self.params.get("volume_ratio", 1.5))

        df["box_high"] = df["high"].rolling(w).max().shift(1)
        df["box_low"] = df["low"].rolling(w).min().shift(1)
        df["vol_ma"] = df["volume"].rolling(w).mean().shift(1)

        df["signal"] = 0

        # 突破箱体上沿买入
        breakout_up = df["close"] > df["box_high"]
        if vol_confirm:
            breakout_up = breakout_up & (df["volume"] > df["vol_ma"] * vol_ratio)
        df.loc[breakout_up, "signal"] = 1

        # 跌破箱体下沿卖出
        breakout_down = df["close"] < df["box_low"]
        if vol_confirm:
            breakout_down = breakout_down & (df["volume"] > df["vol_ma"] * vol_ratio)
        df.loc[breakout_down, "signal"] = -1

        # 置信度：突破幅度 / 箱体高度
        box_height = (df["box_high"] - df["box_low"]).replace(0, 1)
        df["confidence"] = (
            (df["close"] - df["box_high"]).clip(lower=0) / box_height
            + (df["box_low"] - df["close"]).clip(lower=0) / box_height
        ).clip(0, 1)
        return df


class DualThrustStrategy(Strategy):
    """Dual Thrust 突破策略。

    基于 N 日内的最高价-最低价、最高价-收盘价、收盘价-最低价计算上下轨。
    """

    name = "dual_thrust"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        n = int(self.params["lookback"])
        k_up = float(self.params["k_up"])
        k_down = float(self.params["k_down"])

        hh = df["high"].rolling(n).max()
        hc = df["close"].rolling(n).max()
        lc = df["close"].rolling(n).min()
        ll = df["low"].rolling(n).min()

        range_val = np.maximum(hh - lc, hc - ll)
        df["upper"] = df["open"] + k_up * range_val.shift(1)
        df["lower"] = df["open"] - k_down * range_val.shift(1)

        df["signal"] = 0
        df.loc[df["close"] > df["upper"], "signal"] = 1
        df.loc[df["close"] < df["lower"], "signal"] = -1

        df["confidence"] = (
            ((df["close"] - df["upper"]).clip(lower=0) / range_val.replace(0, 1))
            + ((df["lower"] - df["close"]).clip(lower=0) / range_val.replace(0, 1))
        ).clip(0, 1)
        return df
