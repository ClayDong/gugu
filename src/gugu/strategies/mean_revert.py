"""均值回归策略：布林带、RSI、KDJ。"""
from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class BollingerStrategy(Strategy):
    """布林带策略：触及下轨买入，触及上轨卖出。"""

    name = "bollinger"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        w = int(self.params["window"])
        n = float(self.params["num_std"])

        df["ma"] = df["close"].rolling(w).mean()
        df["std"] = df["close"].rolling(w).std()
        df["upper"] = df["ma"] + n * df["std"]
        df["lower"] = df["ma"] - n * df["std"]

        df["signal"] = 0
        # 收盘价跌破下轨买入
        df.loc[df["close"] < df["lower"], "signal"] = 1
        # 收盘价突破上轨卖出
        df.loc[df["close"] > df["upper"], "signal"] = -1

        # 置信度：偏离中轨程度
        band_width = (df["upper"] - df["lower"]).replace(0, 1)
        df["confidence"] = ((df["close"] - df["ma"]).abs() / band_width).clip(0, 1)
        return df


class RSIStrategy(Strategy):
    """RSI 均值回归：超卖买入，超买卖出。"""

    name = "rsi_reversal"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        w = int(self.params["window"])
        oversold = float(self.params["oversold"])
        overbought = float(self.params["overbought"])

        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(w).mean()
        loss = (-delta.clip(upper=0)).rolling(w).mean()
        rs = gain / loss.replace(0, 1e-10)
        df["rsi"] = 100 - (100 / (1 + rs))

        df["signal"] = 0
        # RSI 从超卖区上穿
        df.loc[(df["rsi"] < oversold) & (df["rsi"].shift(1) >= oversold), "signal"] = 1
        # RSI 从超买区下穿
        df.loc[
            (df["rsi"] > overbought) & (df["rsi"].shift(1) <= overbought), "signal"
        ] = -1

        # 置信度：偏离 50 的程度
        df["confidence"] = ((df["rsi"] - 50).abs() / 50).clip(0, 1)
        return df


class KDJStrategy(Strategy):
    """KDJ 策略：K 线金叉 D 线买入，死叉卖出。"""

    name = "kdj"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        w = int(self.params["window"])
        oversold = float(self.params["oversold"])
        overbought = float(self.params["overbought"])

        low_min = df["low"].rolling(w).min()
        high_max = df["high"].rolling(w).max()
        rsv = (df["close"] - low_min) / (high_max - low_min).replace(0, 1) * 100

        df["k"] = rsv.ewm(com=2, adjust=False).mean()
        df["d"] = df["k"].ewm(com=2, adjust=False).mean()
        df["j"] = 3 * df["k"] - 2 * df["d"]

        df["signal"] = 0
        # K 在超卖区金叉 D
        golden = (df["k"] > df["d"]) & (df["k"].shift(1) <= df["d"].shift(1)) & (df["k"] < oversold)
        df.loc[golden, "signal"] = 1
        # K 在超买区死叉 D
        death = (df["k"] < df["d"]) & (df["k"].shift(1) >= df["d"].shift(1)) & (df["k"] > overbought)
        df.loc[death, "signal"] = -1

        df["confidence"] = ((df["j"] - 50).abs() / 50).clip(0, 1)
        return df
