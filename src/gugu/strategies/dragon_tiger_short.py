"""短线主力资金跟随策略（龙虎榜反包形态）。

模拟"机构+游资"短线主力行为：
- 买点：近5日有涨停板 → 断板调整 → 1-3日内阳线反包断板日最高价。
- 卖点：跌破入场止损位 / 跌破 MA20 / 高位放量滞涨 / 时间止损。

注意：龙虎榜数据在 SignalPipeline 的 AltAlphaFilter 中二次确认（lhb_inst_net 等）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class DragonTigerShortStrategy(Strategy):
    """短线涨停断板反包策略。"""

    name = "dragon_tiger_short"
    category = "main_force"

    def validate_params(self) -> None:
        lookback = int(self.params.get("limit_up_lookback", 5))
        if lookback < 1:
            raise ValueError("limit_up_lookback 必须大于 0")
        limit_up = float(self.params.get("limit_up_pct", 0.095))
        if not 0.05 <= limit_up <= 0.11:
            raise ValueError("limit_up_pct 应在 5%-11% 之间")
        if float(self.params.get("max_vol_ratio", 2.0)) < 1.0:
            raise ValueError("max_vol_ratio 必须 >= 1.0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        ma_trend_window = int(self.params.get("ma_trend_window", 20))
        ma_slope_window = int(self.params.get("ma_slope_window", 5))
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        limit_up_lookback = int(self.params.get("limit_up_lookback", 5))
        max_break_days = int(self.params.get("max_break_days", 3))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.05))
        take_profit_pct = float(self.params.get("take_profit_pct", 0.10))
        stagnation_vol_ratio = float(self.params.get("stagnation_vol_ratio", 2.5))
        max_upper_shadow_pct = float(self.params.get("max_upper_shadow_pct", 0.02))
        time_stop_window = int(self.params.get("time_stop_window", 5))
        time_stop_threshold = float(self.params.get("time_stop_threshold", 0.03))
        max_vol_ratio = float(self.params.get("max_vol_ratio", 2.0))
        vol_ma_window = int(self.params.get("vol_ma_window", 20))
        min_confidence = float(self.params.get("min_confidence", 0.55))

        # 均线
        df["ma_trend"] = df["close"].rolling(ma_trend_window).mean()
        df["ma_slope"] = df["ma_trend"].diff(ma_slope_window) / df["ma_trend"].shift(ma_slope_window)

        # 量能
        df["vol_ma"] = df["volume"].rolling(vol_ma_window).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma"].replace(0, np.nan)

        # K线
        df["daily_return"] = df["close"].pct_change()
        df["is_yang"] = df["close"] > df["open"]
        df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["open"].replace(0, np.nan)
        df["is_limit_up"] = np.round(df["daily_return"], 4) >= limit_up_pct

        # 找到最近涨停日
        df["limit_up_day_offset"] = np.nan
        limit_up_indices = df.index[df["is_limit_up"]].tolist()
        for i in range(len(df)):
            prev = [idx for idx in limit_up_indices if idx < i]
            if prev:
                offset = i - prev[-1]
                if offset <= limit_up_lookback:
                    df.loc[df.index[i], "limit_up_day_offset"] = offset

        # 断板日：涨停次日未继续涨停
        df["is_break_day"] = (df["limit_up_day_offset"] == 1) & (df["daily_return"] < limit_up_pct)
        break_indices = df.index[df["is_break_day"]].tolist()

        df["break_day_high"] = np.nan
        df["break_day_low"] = np.nan
        df["break_day_vol_ratio"] = np.nan
        df["limit_up_day_high"] = np.nan
        for i in range(len(df)):
            prev_breaks = [idx for idx in break_indices if idx < i]
            if prev_breaks:
                last_break = prev_breaks[-1]
                if i - last_break <= max_break_days:
                    df.loc[df.index[i], "break_day_high"] = df.loc[df.index[last_break], "high"]
                    df.loc[df.index[i], "break_day_low"] = df.loc[df.index[last_break], "low"]
                    df.loc[df.index[i], "break_day_vol_ratio"] = df.loc[df.index[last_break], "vol_ratio"]
                    lu_idx = last_break - 1
                    if lu_idx >= 0 and df.loc[df.index[lu_idx], "is_limit_up"]:
                        df.loc[df.index[i], "limit_up_day_high"] = df.loc[df.index[lu_idx], "high"]

        # 买点硬条件
        cond_limit_up_before = df["limit_up_day_offset"].notna()
        cond_break_exists = df["break_day_high"].notna()
        cond_rebound = df["close"] > df["break_day_high"]
        cond_yang = df["is_yang"]
        cond_trend_up = (df["ma_slope"] > 0) & (df["close"] > df["ma_trend"])
        cond_not_huge_vol = df["vol_ratio"] <= max_vol_ratio

        core_buy_cond = (
            cond_limit_up_before
            & cond_break_exists
            & cond_rebound
            & cond_yang
            & cond_trend_up
            & cond_not_huge_vol
        )

        # 评分项
        score_break_shrink = (1 - df["break_day_vol_ratio"].clip(0, 1)).fillna(0).clip(0, 1) * 0.25
        score_rebound_vol = (df["vol_ratio"].clip(0, max_vol_ratio) / max_vol_ratio * 0.20).fillna(0)
        score_strong_rebound = (df["close"] > df["limit_up_day_high"]).fillna(False).astype(float) * 0.20
        score_trend_up = (df["ma_slope"].clip(0, 0.05) / 0.05 * 0.15).fillna(0)
        score_timing = ((df["limit_up_day_offset"] >= 2) & (df["limit_up_day_offset"] <= 4)).astype(float) * 0.10
        score_yang = df["is_yang"].astype(float) * 0.10

        df["confidence_score"] = (
            score_break_shrink + score_rebound_vol + score_strong_rebound
            + score_trend_up + score_timing + score_yang
        ).clip(0, 1)

        buy_cond_final = core_buy_cond & (df["confidence_score"] >= min_confidence)

        # 持仓与卖点
        position_window = time_stop_window * 4
        df["in_position"] = buy_cond_final.rolling(window=position_window).sum() > 0
        df["entry_low"] = np.nan
        df["entry_close"] = np.nan
        df.loc[buy_cond_final, "entry_low"] = df.loc[buy_cond_final, "low"]
        df.loc[buy_cond_final, "entry_close"] = df.loc[buy_cond_final, "close"]
        df["entry_low"] = df["entry_low"].ffill()
        df["entry_close"] = df["entry_close"].ffill()

        cond_stop_loss_1 = df["in_position"] & (df["close"] < df["entry_low"] * (1 - stop_loss_pct))
        cond_stop_loss_2 = df["in_position"] & (df["close"] < df["ma_trend"])
        cond_stagnation = df["in_position"] & (
            (df["vol_ratio"] >= stagnation_vol_ratio)
            & (df["upper_shadow"] > max_upper_shadow_pct)
            & (df["daily_return"].abs() < 0.02)
        )
        df["return_since_entry"] = (df["close"] - df["entry_close"]) / df["entry_close"]
        df["days_since_entry"] = 0
        buy_idx_list = df.index[buy_cond_final].tolist()
        for i in range(len(df)):
            prev_buys = [idx for idx in buy_idx_list if idx <= i]
            if prev_buys:
                df.loc[df.index[i], "days_since_entry"] = i - prev_buys[-1]
        cond_time_stop = (
            df["in_position"]
            & (df["days_since_entry"] >= time_stop_window)
            & (df["return_since_entry"] < time_stop_threshold)
        )
        cond_take_profit = df["in_position"] & (df["daily_return"] >= take_profit_pct * 0.6) & df["is_yang"]

        df["signal"] = 0
        df.loc[buy_cond_final, "signal"] = 1
        df.loc[
            cond_stop_loss_1 | cond_stop_loss_2 | cond_stagnation | cond_time_stop | cond_take_profit,
            "signal",
        ] = -1

        df["confidence"] = 0.0
        df.loc[buy_cond_final, "confidence"] = df.loc[buy_cond_final, "confidence_score"].clip(0.55, 0.90)

        return df
