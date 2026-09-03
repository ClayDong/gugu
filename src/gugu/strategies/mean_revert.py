"""均值回归策略：布林带、RSI、KDJ。"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class RSIStrategy(Strategy):
    """RSI 均值回归：超卖买入，超买卖出。"""

    name = "rsi_reversal"
    category = "mean_revert"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
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
        # 2026-09-02 修复：移除 MA20>MA60 硬过滤（与 RSI<20 逻辑矛盾——超卖时必然在下跌趋势中）
        # 改用位置权重：RSI 超卖深度越大，置信度越高（反弹潜力越大）
        rsi_buy = (df["rsi"] >= oversold) & (df["rsi"].shift(1) < oversold)
        df.loc[rsi_buy, "signal"] = 1
        # RSI 从超买区回落
        df.loc[(df["rsi"] <= overbought) & (df["rsi"].shift(1) > overbought), "signal"] = -1

        # 置信度：回升确认时用进入超卖区的深度衡量（shift(1) 时的 RSI 偏离程度）
        prev_rsi = df["rsi"].shift(1)
        buy_conf = (oversold - prev_rsi).clip(lower=0) / oversold
        sell_conf = (prev_rsi - overbought).clip(lower=0) / (100 - overbought)
        df["confidence"] = (buy_conf + sell_conf).clip(0, 1)
        # 无信号时置信度为 0
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        return df
