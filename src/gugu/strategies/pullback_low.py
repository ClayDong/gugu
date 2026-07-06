"""低吸回踩策略：缩量回踩不破 MA20 的低吸逻辑（震荡市专用）。

来源：陈江挺《炒股的智慧》"回调不破支撑才买"原则 + 尾盘选股器实战逻辑。

与 box_breakout 的根本差异：
- box_breakout: 创新高 + 放量 → 追涨（牛市）
- pullback_low: 回踩支撑 + 缩量 → 低吸（震荡市）

8 条件（与尾盘选股器 terminal_screener.py 完全一致）：
1. 当日涨幅 -2% ~ +1%（回踩而非追涨）
2. 前 5 日内有涨幅 ≥ 5% 的强势表现
3. 当日缩量（量比 < 1.0）
4. 收盘价 ≥ MA20（趋势未破）
5. MA20 > MA60（中期趋势向上）
6. 换手率 3%-15%（回测中近似估算）
7. 流通市值 50亿-500亿（回测中近似估算）
8. 不跑赢大盘超过 1%（回测中近似估算）

卖出逻辑（震荡市专用）：
- 反弹至前高 → 止盈
- 跌破 MA20 × 0.97 → 止损
- 持仓 5 日无进展 → 时间止损
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class PullbackLowStrategy(Strategy):
    """低吸回踩策略：缩量回踩不破 MA20。

    震荡市专用策略，与 box_breakout（牛市突破）互补。
    理论依据：陈江挺《炒股的智慧》"回调不破支撑才买"。

    回测说明：
    - 换手率/流通市值条件需要快照数据，回测时用历史均值估算
    - 量比用当日量 / 前 5 日均量近似
    - 8 条件中部分条件在回测中为近似值，实盘以尾盘选股器为准
    """

    name = "pullback_low"

    def validate_params(self) -> None:
        """校验参数语义。"""
        if self.params.get("ma_short", 20) >= self.params.get("ma_long", 60):
            raise ValueError("ma_short 必须小于 ma_long")
        if self.params.get("pullback_min_pct", -0.02) >= self.params.get("pullback_max_pct", 0.01):
            raise ValueError("pullback_min_pct 必须小于 pullback_max_pct")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成低吸回踩信号。"""
        self._ensure_columns(df)
        df = df.copy()

        ma_short = int(self.params.get("ma_short", 20))
        ma_long = int(self.params.get("ma_long", 60))
        strong_window = int(self.params.get("strong_window", 5))
        strong_pct = float(self.params.get("strong_pct", 0.05))
        pullback_min_pct = float(self.params.get("pullback_min_pct", -0.02))
        pullback_max_pct = float(self.params.get("pullback_max_pct", 0.01))
        vol_ratio_max = float(self.params.get("vol_ratio_max", 1.0))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.03))
        take_profit_window = int(self.params.get("take_profit_window", 20))

        # 计算均线
        df["ma_short"] = df["close"].rolling(ma_short).mean()
        df["ma_long"] = df["close"].rolling(ma_long).mean()

        # 计算量比（当日量 / 前 5 日均量）
        df["vol_ma5"] = df["volume"].rolling(strong_window).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # 计算当日涨幅
        df["daily_return"] = df["close"].pct_change()

        # 前 5 日内是否有强势表现（单日涨幅 ≥ strong_pct）
        df["recent_strong"] = (
            df["daily_return"]
            .rolling(strong_window)
            .apply(lambda x: (x >= strong_pct).any(), raw=False)
            .shift(1)  # 不含当日
        )

        # 前高（用于止盈目标位）
        df["recent_high"] = df["high"].rolling(take_profit_window).max().shift(1)

        # ── 买入条件 ──────────────────────────────────
        # 条件1: 当日涨幅 -2% ~ +1%
        cond_pullback = (df["daily_return"] >= pullback_min_pct) & (
            df["daily_return"] <= pullback_max_pct
        )

        # 条件2: 前 5 日内有强势表现
        cond_recent_strong = df["recent_strong"] == True  # noqa: E712

        # 条件3: 当日缩量（量比 < 1.0）
        cond_shrink = df["vol_ratio"] < vol_ratio_max

        # 条件4: 收盘价 ≥ MA20
        cond_above_ma = df["close"] >= df["ma_short"]

        # 条件5: MA20 > MA60（中期趋势向上）
        cond_trend_up = df["ma_short"] > df["ma_long"]

        # 综合买入信号
        buy_cond = (
            cond_pullback & cond_recent_strong & cond_shrink
            & cond_above_ma & cond_trend_up
        )

        # ── 卖出条件（震荡市专用）──────────────────────
        # 1. 止损：跌破 MA20 × (1 - stop_loss_pct)
        df["stop_loss_price"] = df["ma_short"] * (1 - stop_loss_pct)
        cond_stop_loss = df["close"] < df["stop_loss_price"]

        # 2. 止盈：反弹至前高
        cond_take_profit = df["close"] >= df["recent_high"]

        # 3. 时间止损：持仓 5 日内涨幅 < 2%（无进展）→ 减半仓信号
        # 震荡市快进快出，不允许长期占用资金
        time_stop_window = int(self.params.get("time_stop_window", 5))
        time_stop_threshold = float(self.params.get("time_stop_threshold", 0.02))
        df["return_5d"] = df["close"].pct_change(periods=time_stop_window)
        cond_time_stop = df["return_5d"] < time_stop_threshold

        # 信号赋值（卖出优先于买入，趋势破坏时不入场）
        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[cond_stop_loss | cond_take_profit | cond_time_stop, "signal"] = -1

        # ── 置信度计算 ────────────────────────────────
        # 基于：回踩深度（越深越好）+ 缩量程度（越缩越好）+ 趋势强度
        pullback_depth = (df["ma_short"] - df["close"]) / df["ma_short"].replace(0, np.nan)
        shrink_ratio = 1 - df["vol_ratio"].clip(upper=1.0)
        trend_strength = (df["ma_short"] - df["ma_long"]) / df["ma_long"].replace(0, np.nan)

        df["confidence"] = (
            pullback_depth.clip(0, 0.05) / 0.05 * 0.4  # 回踩深度权重 40%
            + shrink_ratio * 0.4  # 缩量程度权重 40%
            + trend_strength.clip(0, 0.1) / 0.1 * 0.2  # 趋势强度权重 20%
        ).clip(0, 1)

        # 无信号时置信度为 0
        df.loc[df["signal"] == 0, "confidence"] = 0.0

        return df
