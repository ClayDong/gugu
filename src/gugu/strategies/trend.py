"""趋势跟踪策略：海龟、双均线、MACD。"""
from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class TurtleStrategy(Strategy):
    """海龟交易系统：唐奇安通道突破 + ATR 动态止损。"""

    name = "turtle"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        bw = int(self.params["breakout_window"])
        ew = int(self.params["exit_window"])
        aw = int(self.params["atr_window"])

        df["upper"] = df["high"].rolling(bw).max().shift(1)
        df["lower"] = df["low"].rolling(bw).min().shift(1)
        # 海龟出场线：多头跌破近 N 日最低点
        df["exit_low"] = df["low"].rolling(ew).min().shift(1)
        df["atr"] = self._atr(df, aw)

        df["signal"] = 0
        # 突破上轨买入
        df.loc[df["close"] > df["upper"], "signal"] = 1
        # 跌破出场线卖出
        df.loc[df["close"] < df["exit_low"], "signal"] = -1

        # 置信度：突破幅度 / ATR
        df["confidence"] = (
            (df["close"] - df["upper"]).clip(lower=0) / df["atr"].replace(0, 1)
        ).clip(0, 1)
        return df


class DualMAStrategy(Strategy):
    """双均线交叉：短均线上穿长均线买入，下穿卖出。"""

    name = "dual_ma"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        sw = int(self.params["short_window"])
        lw = int(self.params["long_window"])

        df["ma_short"] = df["close"].rolling(sw).mean()
        df["ma_long"] = df["close"].rolling(lw).mean()

        df["signal"] = 0
        # 金叉买入
        golden = (df["ma_short"] > df["ma_long"]) & (
            df["ma_short"].shift(1) <= df["ma_long"].shift(1)
        )
        df.loc[golden, "signal"] = 1
        # 死叉卖出
        death = (df["ma_short"] < df["ma_long"]) & (
            df["ma_short"].shift(1) >= df["ma_long"].shift(1)
        )
        df.loc[death, "signal"] = -1

        # 置信度：均线距离占比
        df["confidence"] = (
            (df["ma_short"] - df["ma_long"]).abs() / df["close"].replace(0, 1)
        ).clip(0, 1)
        return df


class MACDStrategy(Strategy):
    """MACD 策略：金叉买入，死叉卖出。"""

    name = "macd"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        sw = int(self.params["short_window"])
        lw = int(self.params["long_window"])
        sig_w = int(self.params["signal_window"])

        ema_short = df["close"].ewm(span=sw, adjust=False).mean()
        ema_long = df["close"].ewm(span=lw, adjust=False).mean()
        df["macd"] = ema_short - ema_long
        df["macd_signal"] = df["macd"].ewm(span=sig_w, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]

        df["signal"] = 0
        golden = (df["macd"] > df["macd_signal"]) & (
            df["macd"].shift(1) <= df["macd_signal"].shift(1)
        )
        df.loc[golden, "signal"] = 1
        death = (df["macd"] < df["macd_signal"]) & (
            df["macd"].shift(1) >= df["macd_signal"].shift(1)
        )
        df.loc[death, "signal"] = -1

        df["confidence"] = (df["macd_hist"].abs() / df["close"].replace(0, 1)).clip(0, 1)
        return df
