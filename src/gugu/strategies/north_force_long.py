"""长线主力资金跟随策略。

模拟"北向+公募"类长线主力行为：
- 买点：中期趋势向上 + 缩量回踩 MA20 + 近期有资金关注 + 非历史天量。
- 卖点：跌破 MA20 / 趋势反转 / 高位放量滞涨 / 时间止损。

注意：本策略仅用 OHLCV 识别主力行为形态；生产环境中，SignalPipeline 的
AltAlphaFilter（高管增持/龙虎榜/融资情绪）和 HolderConcentrationFilter
会对其进行二次过滤，近似实现"跟随主力"。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class NorthForceLongStrategy(Strategy):
    """长线吸筹跟随策略。"""

    name = "north_force_long"
    category = "main_force"

    def validate_params(self) -> None:
        """校验参数语义。"""
        ma_trend = int(self.params.get("ma_trend_window", 20))
        ma_long = int(self.params.get("ma_long_window", 60))
        if ma_trend >= ma_long:
            raise ValueError("ma_trend_window 必须小于 ma_long_window")
        if float(self.params.get("pullback_to_ma_pct", 0.02)) <= 0:
            raise ValueError("pullback_to_ma_pct 必须 > 0")
        if float(self.params.get("max_vol_ratio", 1.5)) < 1.0:
            raise ValueError("max_vol_ratio 必须 >= 1.0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成长线主力吸筹跟随信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # 参数
        ma_trend_window = int(self.params.get("ma_trend_window", 20))
        ma_long_window = int(self.params.get("ma_long_window", 60))
        ma_slope_window = int(self.params.get("ma_slope_window", 5))
        pullback_to_ma_pct = float(self.params.get("pullback_to_ma_pct", 0.02))
        recent_momentum_window = int(self.params.get("recent_momentum_window", 20))
        recent_momentum_pct = float(self.params.get("recent_momentum_pct", 0.05))
        recent_spike_window = int(self.params.get("recent_spike_window", 5))
        recent_spike_pct = float(self.params.get("recent_spike_pct", 0.05))
        max_daily_gain_pct = float(self.params.get("max_daily_gain_pct", 0.02))
        max_vol_ratio = float(self.params.get("max_vol_ratio", 1.5))
        vol_ma_window = int(self.params.get("vol_ma_window", 60))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.07))
        take_profit_pct = float(self.params.get("take_profit_pct", 0.20))
        stagnation_vol_ratio = float(self.params.get("stagnation_vol_ratio", 2.5))
        max_upper_shadow_pct = float(self.params.get("max_upper_shadow_pct", 0.03))
        time_stop_window = int(self.params.get("time_stop_window", 30))
        time_stop_threshold = float(self.params.get("time_stop_threshold", 0.05))
        min_confidence = float(self.params.get("min_confidence", 0.55))

        # 持仓状态窗口：买入后允许持有的最大天数 + 缓冲
        # 用状态机精确追踪，避免固定滚动窗口导致卖出泛滥
        position_window = max(time_stop_window + 10, 20)

        # 数据长度校验：覆盖所有滚动/状态窗口
        min_len = max(ma_long_window, vol_ma_window, recent_momentum_window, position_window) + 2
        if len(df) < min_len:
            df["signal"] = 0
            df["confidence"] = 0.0
            return df

        # 均线与斜率
        df["ma_trend"] = df["close"].rolling(ma_trend_window).mean()
        df["ma_long"] = df["close"].rolling(ma_long_window).mean()
        df["ma_slope"] = df["ma_trend"].diff(ma_slope_window) / df["ma_trend"].shift(
            ma_slope_window
        )

        # 量能
        df["vol_ma"] = df["volume"].rolling(vol_ma_window).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma"].replace(0, np.nan)

        # 基础 K 线
        df["daily_return"] = df["close"].pct_change()
        df["is_yang"] = df["close"] > df["open"]
        df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["open"].replace(
            0, np.nan
        )

        # 买点硬条件
        cond_trend_up = (df["ma_trend"] > df["ma_long"]) & (df["ma_slope"] > 0)
        cond_above_ma = df["close"] > df["ma_trend"]
        cond_pullback = (df["close"] - df["ma_trend"]) / df["ma_trend"] <= pullback_to_ma_pct
        cond_not_crazy = df["daily_return"] <= max_daily_gain_pct
        cond_not_huge_vol = df["vol_ratio"] <= max_vol_ratio

        # 近期有资金关注
        momentum = df["close"] / df["close"].shift(recent_momentum_window) - 1.0
        cond_momentum = momentum >= recent_momentum_pct
        recent_spike = (
            df["daily_return"].rolling(recent_spike_window).max().shift(1) >= recent_spike_pct
        )
        cond_recent_attention = cond_momentum | recent_spike

        core_buy_cond = (
            cond_trend_up
            & cond_above_ma
            & cond_pullback
            & cond_not_crazy
            & cond_not_huge_vol
            & cond_recent_attention
        )

        # 评分项
        score_pullback = ((df["close"] - df["ma_trend"]) / df["ma_trend"]).clip(
            0, pullback_to_ma_pct
        )
        score_pullback = (1 - score_pullback / pullback_to_ma_pct) * 0.25
        score_momentum = (momentum.clip(0, 0.20) / 0.20 * 0.25).fillna(0)
        score_trend_slope = (df["ma_slope"].clip(0, 0.05) / 0.05 * 0.25).fillna(0)
        score_vol_healthy = (
            (1 - df["vol_ratio"].clip(0, max_vol_ratio) / max_vol_ratio) * 0.15
        ).fillna(0)
        score_yang = df["is_yang"].astype(float) * 0.10

        df["confidence_score"] = (
            score_pullback + score_momentum + score_trend_slope + score_vol_healthy + score_yang
        ).clip(0, 1)

        buy_cond_final = core_buy_cond & (df["confidence_score"] >= min_confidence)

        # 卖点序列（先计算，状态机里引用）
        # 跌破 MA20 止损：要求连续 2 日收盘低于 MA20，避免单日噪音过早止损
        cond_stop_loss_2 = (df["close"] < df["ma_trend"]) & (
            df["close"].shift(1) < df["ma_trend"].shift(1)
        )
        cond_ma_death_cross = (df["ma_trend"] < df["ma_long"]) & (
            df["ma_trend"].shift(1) >= df["ma_long"].shift(1)
        )
        cond_stagnation = (
            (df["vol_ratio"] >= stagnation_vol_ratio)
            & (df["upper_shadow"] > max_upper_shadow_pct)
            & (df["daily_return"].abs() < 0.02)
        )
        cond_take_profit = (df["daily_return"] >= take_profit_pct * 0.6) & df["is_yang"]

        # 状态机精确追踪持仓，避免固定滚动窗口导致卖出泛滥
        in_position = False
        entry_idx = -1
        df["in_position"] = False
        df["entry_low"] = np.nan
        df["entry_close"] = np.nan
        df["days_since_entry"] = 0
        df["return_since_entry"] = np.nan
        sell_cond = pd.Series(False, index=df.index)

        for i in range(len(df)):
            if not in_position:
                if buy_cond_final.iloc[i]:
                    in_position = True
                    entry_idx = i
                    df.loc[df.index[i], "entry_low"] = df["low"].iloc[i]
                    df.loc[df.index[i], "entry_close"] = df["close"].iloc[i]
            else:
                df.loc[df.index[i], "days_since_entry"] = i - entry_idx
                ret_since = (df["close"].iloc[i] - df["entry_close"].iloc[entry_idx]) / df[
                    "entry_close"
                ].iloc[entry_idx]
                df.loc[df.index[i], "return_since_entry"] = ret_since
                entry_low_val = df["entry_low"].iloc[entry_idx]
                cond_stop_loss_1_i = df["close"].iloc[i] < entry_low_val * (1 - stop_loss_pct)
                cond_time_stop_i = (i - entry_idx >= time_stop_window) and (
                    ret_since < time_stop_threshold
                )
                sell_now = (
                    cond_stop_loss_1_i
                    or cond_stop_loss_2.iloc[i]
                    or cond_ma_death_cross.iloc[i]
                    or cond_stagnation.iloc[i]
                    or cond_take_profit.iloc[i]
                    or cond_time_stop_i
                )
                if sell_now:
                    sell_cond.iloc[i] = True
                    in_position = False
                    entry_idx = -1
            df.loc[df.index[i], "in_position"] = in_position

        df["entry_low"] = df["entry_low"].ffill()
        df["entry_close"] = df["entry_close"].ffill()

        # 信号赋值：卖出优先
        df["signal"] = 0
        df.loc[buy_cond_final, "signal"] = 1
        df.loc[sell_cond, "signal"] = -1

        df["confidence"] = 0.0
        df.loc[buy_cond_final, "confidence"] = df.loc[buy_cond_final, "confidence_score"].clip(
            0.55, 0.85
        )

        return df
