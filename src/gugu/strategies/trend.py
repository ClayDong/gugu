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
        df["exit_low"] = df["low"].rolling(ew).min().shift(1)
        df["atr"] = self._atr(df, aw)

        df["signal"] = 0
        # 突破上轨买入
        df.loc[df["close"] > df["upper"], "signal"] = 1
        # 跌破出场线卖出
        df.loc[df["close"] < df["exit_low"], "signal"] = -1

        # 置信度：买入用突破上轨幅度，卖出用跌破出场线幅度
        atr_safe = df["atr"].replace(0, 1)
        buy_conf = (df["close"] - df["upper"]).clip(lower=0) / atr_safe
        sell_conf = (df["exit_low"] - df["close"]).clip(lower=0) / atr_safe
        df["confidence"] = (buy_conf + sell_conf).clip(0, 1)
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

        # 置信度：交叉信号本身是强信号，用交叉斜率衡量确定性
        # 金叉/死叉瞬间差值极小，用差值的变化率（斜率）更合理
        ma_diff = df["ma_short"] - df["ma_long"]
        ma_slope = ma_diff.diff().abs()
        price_safe = df["close"].replace(0, 1)
        # 基线 0.65 + 斜率贡献（最大 0.35），确保交叉信号置信度 ≥ 0.65
        df["confidence"] = (0.65 + (ma_slope / price_safe * 50).clip(0, 0.35)).clip(0, 1)
        # 无信号时置信度为 0
        df.loc[df["signal"] == 0, "confidence"] = 0.0
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

        # 置信度：交叉信号用基线 + 柱状图斜率，与 DualMA 同理
        hist_slope = df["macd_hist"].diff().abs()
        price_safe = df["close"].replace(0, 1)
        df["confidence"] = (0.65 + (hist_slope / price_safe * 50).clip(0, 0.35)).clip(0, 1)
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        return df
