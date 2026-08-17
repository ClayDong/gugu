"""5日均线动量策略：放量大阳线启动 + 5日均线趋势跟踪。

核心规则（来自用户交易心法）：
1. 没有放量大阳线坚决不碰
2. 放量之后股价回踩5日均线但没有突破，是重点观察位置；均线没有失手，放量看方向，回踩看位置
3. 股价明显偏离5日线，先兑现一部分利润
4. 收盘跌破5日均线，先降一部分仓位
5. 跌破均线后连续三个交易日站不回去，直接离场
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class MA5MomentumStrategy(Strategy):
    """5日均线动量策略。

    买入：近 N 日出现过放量大阳线，当前回踩 MA5 但未跌破，且 MA5 向上。
    卖出：偏离 MA5 达阈值部分止盈；收盘跌破 MA5 减仓；连续3日站不回 MA5 清仓。
    """

    name = "ma5_momentum"
    category = "trend"

    def validate_params(self) -> None:
        positive_params = (
            "big_yang_lookback",
            "vol_ma_window",
            "ma5_break_exit_days",
        )
        for name in positive_params:
            if int(self.params.get(name, 0)) <= 0:
                raise ValueError(f"{name} 必须大于 0")
        for name in (
            "big_yang_gain_pct",
            "big_yang_vol_ratio",
            "pullback_tolerance",
            "shrink_vol_ratio",
            "profit_deviation_pct",
        ):
            if float(self.params.get(name, 0)) < 0:
                raise ValueError(f"{name} 不能小于 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        big_yang_gain_pct = float(self.params["big_yang_gain_pct"])
        big_yang_vol_ratio = float(self.params["big_yang_vol_ratio"])
        big_yang_lookback = int(self.params["big_yang_lookback"])
        pullback_tolerance = float(self.params["pullback_tolerance"])
        shrink_vol_ratio = float(self.params["shrink_vol_ratio"])
        profit_deviation_pct = float(self.params["profit_deviation_pct"])
        ma5_break_exit_days = int(self.params["ma5_break_exit_days"])
        vol_ma_window = int(self.params["vol_ma_window"])

        df["ma5"] = df["close"].rolling(5).mean()
        df["ma5_slope"] = df["ma5"] - df["ma5"].shift(1)
        df["vol_ma"] = df["volume"].rolling(vol_ma_window).mean()

        gain = df["close"].pct_change()
        df["gain"] = gain
        is_big_yang = gain >= big_yang_gain_pct
        vol_ratio = df["volume"] / df["vol_ma"].replace(0, np.nan)
        is_volume_surge = vol_ratio >= big_yang_vol_ratio
        df["is_big_yang"] = is_big_yang & is_volume_surge

        # 近 lookback 日内是否出现过放量大阳线
        df["recent_big_yang"] = (
            df["is_big_yang"].shift(1).rolling(big_yang_lookback).max().fillna(0).astype(bool)
        )

        # 回踩 MA5 条件：收盘价 >= MA5 * (1 - tolerance) 且未跌破 MA5
        df["pullback_ok"] = df["close"] >= df["ma5"] * (1 - pullback_tolerance)
        df["not_break_ma5"] = df["close"] >= df["ma5"]

        # 缩量确认：回踩日成交量 <= 放量日的一定比例（默认 <= 前5日均量 * 0.7）
        df["shrink_ok"] = vol_ratio <= shrink_vol_ratio

        # 买入信号
        df["signal"] = 0
        buy_cond = (
            df["recent_big_yang"]
            & df["pullback_ok"]
            & df["not_break_ma5"]
            & df["shrink_ok"]
            & (df["ma5_slope"] > 0)
        )
        df.loc[buy_cond, "signal"] = 1

        # 卖出信号
        # 1. 偏离 MA5 止盈（部分减仓信号，这里用 -1 表示，由执行层决定减仓比例）
        df["deviation_from_ma5"] = (df["close"] - df["ma5"]) / df["ma5"].replace(0, np.nan)
        df["over_deviated"] = df["deviation_from_ma5"] >= profit_deviation_pct

        # 2. 收盘跌破 MA5
        df["below_ma5"] = df["close"] < df["ma5"]

        # 3. 连续 N 日站不回 MA5
        df["below_ma5_streak"] = self._consecutive_below_ma5(df)
        df["force_exit"] = df["below_ma5_streak"] >= ma5_break_exit_days

        # 卖出优先级：强制离场 > 跌破 MA5 > 偏离止盈
        # 只在非买入日触发卖出，避免同一天买卖信号冲突
        no_buy = ~buy_cond
        # 强制离场
        df.loc[no_buy & df["force_exit"], "signal"] = -1
        # 跌破 MA5（未触发强制离场时）
        df.loc[no_buy & df["below_ma5"] & ~df["force_exit"], "signal"] = -1
        # 偏离止盈（未触发更高级别卖出时）
        df.loc[no_buy & df["over_deviated"] & ~df["below_ma5"] & ~df["force_exit"], "signal"] = -1

        # 置信度
        # 买入：基于回踩幅度和 MA5 斜率
        pullback_depth = (df["close"] / df["ma5"] - 1).clip(upper=0).abs()
        conf_from_pullback = (pullback_depth / pullback_tolerance).clip(0, 1)
        conf_from_slope = (df["ma5_slope"] / df["ma5"]).clip(lower=0) * 100
        conf_from_slope = conf_from_slope.clip(0, 1)
        df["buy_conf"] = ((conf_from_pullback + conf_from_slope) / 2).clip(0, 1)

        # 卖出：基于偏离程度
        df["sell_conf"] = df["deviation_from_ma5"].clip(lower=0) / profit_deviation_pct
        df["sell_conf"] = df["sell_conf"].clip(0, 1)

        df["confidence"] = 0.0
        df.loc[df["signal"] == 1, "confidence"] = df.loc[df["signal"] == 1, "buy_conf"]
        df.loc[df["signal"] == -1, "confidence"] = df.loc[df["signal"] == -1, "sell_conf"]

        return df

    @staticmethod
    def _consecutive_below_ma5(df: pd.DataFrame) -> pd.Series:
        """计算连续收盘跌破 MA5 的天数。

        当天收盘 >= MA5 时，计数归零；当天收盘 < MA5 时，计数 +1。
        """
        below = (df["close"] < df["ma5"]).to_numpy(dtype=bool)
        streak = np.zeros(len(below), dtype=int)
        count = 0
        for i, is_below in enumerate(below):
            if is_below:
                count += 1
            else:
                count = 0
            streak[i] = count
        return pd.Series(streak, index=df.index)
