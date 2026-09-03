"""四周期市场模型策略（公众号《八月，来了~~》战法）。

核心认知（原文提炼）：
- 股价站上20日均线形成「银山谷」（MA20上穿MA60）→ 潜伏期，控制仓位耐心等待
- 放量站上60日均线 → 醒悟期，分批加仓滚动操作
- >45度拉升 → 狂热期，大涨大卖获利了结
- 顶背离跌破20日均线形成「死亡谷」（MA20下穿MA60）→ 幻灭期，降低仓位获利了结
- MACD 死叉状态不会有主升浪，主升浪必须建立在 MACD 金叉状态

策略：
1. regime_cycle_four —— 四周期识别（潜伏期买入 / 幻灭期卖出）
"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


def _macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9):
    """计算 MACD，返回 (dif, dea, hist)。"""
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2
    return dif, dea, hist


class RegimeCycleFourStrategy(Strategy):
    """四周期模型：潜伏期买入（银山谷），幻灭期卖出（死亡谷）。

    原文判据：
    - 买入（潜伏期确认）：站上20日均线 + 银山谷（MA20上穿MA60）+ MACD金叉
    - 卖出（幻灭期确认）：跌破20日均线 + 死亡谷（MA20下穿MA60）+ MACD死叉
    - 狂热期（>45度拉升）：视为持仓信号增强，不单独触发卖出（卖出以幻灭期为准）
    """

    name = "regime_cycle_four"
    category = "trend"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        ma_short = int(self.params["ma_short"])
        ma_long = int(self.params["ma_long"])
        macd_fast = int(self.params["macd_fast"])
        macd_slow = int(self.params["macd_slow"])
        macd_signal = int(self.params["macd_signal"])
        # 2026-09-02 新增：银山谷窗口——金叉后 N 日内均可入场（原只允许当天）
        silver_window = int(self.params.get("silver_window", 5))

        df["ma_short"] = df["close"].rolling(ma_short).mean()
        df["ma_long"] = df["close"].rolling(ma_long).mean()
        dif, dea, _ = _macd(df, macd_fast, macd_slow, macd_signal)
        df["macd_dif"] = dif
        df["macd_dea"] = dea

        # 银山谷：MA20 上穿 MA60（潜伏期开始）
        # 2026-09-02 修复：金叉后 N 日内均可入场（原只允许当天，导致信号极其稀少）
        silver_valley_cross = (df["ma_short"] > df["ma_long"]) & (
            df["ma_short"].shift(1) <= df["ma_long"].shift(1)
        )
        silver_valley = silver_valley_cross.rolling(silver_window, min_periods=1).max().astype(bool)
        # 死亡谷：MA20 下穿 MA60（幻灭期开始）
        death_valley = (df["ma_short"] < df["ma_long"]) & (
            df["ma_short"].shift(1) >= df["ma_long"].shift(1)
        )

        # MACD 状态（原文语义：主升浪必须建立在 MACD 金叉状态，死叉状态无主升浪）
        macd_golden_state = dif > dea
        macd_dead_state = dif < dea

        # 买入：银山谷（多头排列确立）+ 站上20日线 + MACD 处于金叉状态
        buy_cond = silver_valley & (df["close"] > df["ma_short"]) & macd_golden_state
        # 卖出：死亡谷（空头排列确立）+ 跌破20日线 + MACD 处于死叉状态
        sell_cond = death_valley & (df["close"] < df["ma_short"]) & macd_dead_state

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[sell_cond, "signal"] = -1

        # 置信度：金叉/死叉的 DIF-DEA 距离归一化
        macd_spread = (dif - dea).abs()
        df["confidence"] = (macd_spread / df["close"] * 100).clip(0, 1)
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        return df
