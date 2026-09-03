"""板块动量轮动策略（sector_momentum）。

A 股核心特征：板块轮动时，龙头股领涨且持续性强（"强者恒强"）。
本策略捕捉个股动量效应，用 trend + momentum + volume 三重确认：

- 买入：短期趋势向上（MA5>MA10）+ 中期趋势确认（close>MA20）
         + 近期有动量（5日收益>0）+ 放量确认（volume>1.2×20日均量）
- 卖出：跌破 MA10 或 从最高点回撤 8% 止损

设计原则：
- 所有条件用 OHLCV 日线数据，可回测，不依赖实时行业资金流
- 参数化核心阈值，支持 Walk-Forward 优化
- 比 box_breakout 更简单直接，比 rsi_reversal 更符合 A 股动量特征
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()

__all__ = ["SectorMomentumStrategy"]


class SectorMomentumStrategy(Strategy):
    """板块动量轮动策略。

    核心思想：不预测，只跟随。当股票展现出趋势+动量+放量时入场，
    趋势走弱或回撤过大时离场。

    买入条件（4 AND）：
    1. MA5 > MA10（短期趋势向上）
    2. close > MA20（中期趋势确认）
    3. 5日收益 > 0（近期有动量）
    4. volume > 1.2 × MA20 volume（放量确认）

    卖出条件（2 OR）：
    1. close < MA10（短期趋势走弱）
    2. close < 入场以来最高价 × 0.92（回撤 8% 止损）
    """

    name = "sector_momentum"
    category = "trend"

    def validate_params(self) -> None:
        """校验参数语义。"""
        if int(self.params.get("ma_short", 5)) >= int(self.params.get("ma_mid", 20)):
            raise ValueError("ma_short 必须小于 ma_mid")
        if float(self.params.get("stop_loss_pct", 0.08)) <= 0:
            raise ValueError("stop_loss_pct 必须 > 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        ma_short = int(self.params.get("ma_short", 5))
        ma_mid = int(self.params.get("ma_mid", 20))
        momentum_days = int(self.params.get("momentum_days", 5))
        vol_ratio = float(self.params.get("vol_ratio", 1.2))
        max_hold_days = int(self.params.get("max_hold_days", 20))

        # ── 计算均线 ──────────────────────────────────
        df["ma_short"] = df["close"].rolling(ma_short).mean()
        df["ma_mid"] = df["close"].rolling(ma_mid).mean()
        df["vol_ma_mid"] = df["volume"].rolling(ma_mid).mean().shift(1)

        # ── 买入条件（4 AND）──────────────────────────
        c1_trend = df["ma_short"] > df["ma_short"].shift(1)  # MA5 斜率向上
        c2_above_ma = df["close"] > df["ma_mid"]  # 站上 MA20
        c3_momentum = df["close"].pct_change(momentum_days) > 0  # N 日正收益
        c4_volume = df["volume"] > df["vol_ma_mid"] * vol_ratio  # 放量

        buy_cond = c1_trend & c2_above_ma & c3_momentum & c4_volume

        # ── 卖出条件（持仓后才生效）──────────────────
        # 跌破 MA5 → 卖出。2026-09-03 修复（评审 P3）：原逻辑无条件产生卖出
        # （1365 信号中 1144 个是卖出，无持仓也触发）。现在只在买入后的持仓区间
        # 允许卖出；移动止损由基类 _apply_max_hold_exit 处理（trailing_stop_pct）。
        c_sell_ma = df["close"] < df["ma_short"]  # 跌破 MA5

        # 持仓感知：买入开仓 → 卖出平仓 → 再买入再开仓
        buy_arr = buy_cond.to_numpy(dtype=bool)
        sell_raw = c_sell_ma.to_numpy(dtype=bool)
        in_pos = False
        valid_sell = np.zeros(len(df), dtype=bool)
        for i in range(len(df)):
            if buy_arr[i]:
                in_pos = True
            if sell_raw[i] and in_pos:
                valid_sell[i] = True
                in_pos = False
        sell_cond = c_sell_ma & pd.Series(valid_sell, index=df.index)

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[sell_cond, "signal"] = -1

        # ── 置信度：动量强度 + 量能强度 ────────────────
        momentum_strength = df["close"].pct_change(momentum_days).clip(lower=0)
        vol_strength = (df["volume"] / df["vol_ma_mid"].replace(0, np.nan)).clip(1, 3)
        df["confidence"] = 0.0
        df.loc[buy_cond, "confidence"] = (
            (momentum_strength / 0.05).clip(0, 1) * 0.5 + ((vol_strength - 1) / 2).clip(0, 1) * 0.5
        ).clip(0, 1)
        df.loc[sell_cond, "confidence"] = 0.6

        # ── 强制出场 ──────────────────────────────────
        df = self._apply_max_hold_exit(df, max_hold_days=max_hold_days)

        buy_count = int(np.sum(df["signal"] == 1))
        sell_count = int(np.sum(df["signal"] == -1))
        last_signal = int(df["signal"].iloc[-1]) if len(df) else 0
        last_signal_desc = {1: "买入", -1: "卖出", 0: "无"}.get(last_signal, "无")
        logger.info(
            f"sector_momentum 信号统计: {buy_count} 买入, {sell_count} 卖出 | "
            f"当日最新信号: {last_signal_desc}"
        )

        return df
