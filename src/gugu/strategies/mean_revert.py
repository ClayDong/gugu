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

        # 置信度：基线 0.5（触轨即信号）+ 偏离布林带外的程度
        band_width = (df["upper"] - df["lower"]).clip(lower=df["close"].abs() * 0.01).replace(0, 1)
        outside_buy = (df["lower"] - df["close"]).clip(lower=0) / band_width
        outside_sell = (df["close"] - df["upper"]).clip(lower=0) / band_width
        df["confidence"] = (0.5 + outside_buy + outside_sell).clip(0, 1)
        # 无信号时置信度为 0
        df.loc[df["signal"] == 0, "confidence"] = 0.0
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
        # 零波动时 RSI 应为 50（中性）
        both_zero = (gain.abs() < 1e-10) & (loss.abs() < 1e-10)
        rs = gain / loss.replace(0, 1e-10)
        rs[both_zero] = 1.0
        df["rsi"] = 100 - (100 / (1 + rs))

        df["signal"] = 0
        # RSI 回升确认策略：从超卖区回升到超卖线上方时买入，从超买区回落到超买线下方时卖出
        # 回升确认流派：RSI 离开超卖区时触发（价格已反弹），更安全
        df.loc[(df["rsi"] >= oversold) & (df["rsi"].shift(1) < oversold), "signal"] = 1
        # RSI 从超买区回落
        df.loc[
            (df["rsi"] <= overbought) & (df["rsi"].shift(1) > overbought), "signal"
        ] = -1

        # 置信度：回升确认时用进入超卖区的深度衡量（shift(1) 时的 RSI 偏离程度）
        prev_rsi = df["rsi"].shift(1)
        buy_conf = ((oversold - prev_rsi).clip(lower=0) / oversold)
        sell_conf = ((prev_rsi - overbought).clip(lower=0) / (100 - overbought))
        df["confidence"] = (buy_conf + sell_conf).clip(0, 1)
        # 无信号时置信度为 0
        df.loc[df["signal"] == 0, "confidence"] = 0.0
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
        price_range = high_max - low_min
        # 零波动时 RSV 设为 50（中性）
        rsv = pd.Series(50.0, index=df.index)
        tradable = price_range > 0
        rsv[tradable] = (df["close"][tradable] - low_min[tradable]) / price_range[tradable] * 100

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

        # 置信度：用 K 值在超卖/超买区的深度衡量
        buy_conf = ((oversold - df["k"]).clip(lower=0) / oversold)
        sell_conf = ((df["k"] - overbought).clip(lower=0) / (100 - overbought))
        df["confidence"] = (buy_conf + sell_conf).clip(0, 1)
        # 无信号时置信度为 0
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        return df
