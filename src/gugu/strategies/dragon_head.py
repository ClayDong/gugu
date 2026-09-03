"""龙头战法策略（dragon_head）。

钱多多龙头战法：
- 只做市场选出的龙头，不做自己选的龙头
- 四梁八柱健全时入场（中军向上 + 跟随龙涨停 + 老龙不破支撑）
- 回踩低吸，情绪破位离场

回测模式：用个股自身动量特征近似龙头判断（涨停/连板/量能扩张/趋势强度）。
生产模式：由 DragonHeadDetector 跨股分析结果注入龙头信息。

核心逻辑：
- 买入：龙头特征成立（涨停/连板/趋势强/量能扩张）+ 回踩不破 MA5
- 卖出：龙头特征消失（不再涨停/趋势走弱/量能萎缩）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()

__all__ = ["DragonHeadStrategy"]


class DragonHeadStrategy(Strategy):
    """龙头战法策略。

    道：只做龙头，只看龙头。龙-空-龙。
    术：龙头特征四维确认 + 情绪周期判断 + 回踩低吸。

    买入条件（4 AND）：
    1. 龙头特征：近10日有涨停（涨幅>9.5%）或连板
    2. 趋势确认：收盘 > MA5 且 MA5 斜率向上
    3. 量能确认：近5日均量 > 20日均量（资金活跃）
    4. 回踩低吸：今日非涨停（涨幅<7%），在MA5附近（乖离<3%）

    卖出条件（3 OR）：
    1. 龙头破位：收盘 < MA5 且 MA5 斜率向下
    2. 量能枯竭：近5日均量 < 20日均量 × 0.7
    3. 移动止损：从最高点回撤 8%
    """

    name = "dragon_head"
    category = "trend"

    def validate_params(self) -> None:
        if int(self.params.get("ma_short", 5)) <= 0:
            raise ValueError("ma_short 必须 > 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        ma_short = int(self.params.get("ma_short", 5))
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        pullback_max = float(self.params.get("pullback_max", 0.03))
        vol_expand_min = float(self.params.get("vol_expand_min", 1.0))
        vol_shrink_exit = float(self.params.get("vol_shrink_exit", 0.7))
        trailing_stop_pct = float(self.params.get("trailing_stop_pct", 0.08))

        # ── 基础指标 ──────────────────────────────────
        df["ma_short"] = df["close"].rolling(ma_short).mean()
        df["ma_short_slope"] = df["ma_short"].diff(ma_short) / df["ma_short"].shift(ma_short)
        df["daily_ret"] = df["close"].pct_change()
        df["vol_ma5"] = df["volume"].rolling(5).mean()
        df["vol_ma20"] = df["volume"].rolling(20).mean()
        df["vol_ratio"] = df["vol_ma5"] / df["vol_ma20"].replace(0, np.nan)

        # ── 龙头特征检测 ──────────────────────────────
        # 1. 强势检测：近10日出现过单日涨幅 > 7%（接近涨停，龙头特征）
        is_strong_day = df["daily_ret"] > limit_up_pct
        has_strong_day_10d = is_strong_day.rolling(10, min_periods=1).max().astype(bool)
        # 2. 连续强势（连板近似：连续2日涨幅 > 5%）
        consecutive_strong = (df["daily_ret"] > 0.05) & (df["daily_ret"].shift(1) > 0.05)
        # 3. 龙头特征：近10日有强势日 OR 连续强势
        is_dragon_like = has_strong_day_10d | consecutive_strong

        # ── 买入条件（4 AND）──────────────────────────
        # 龙头特征
        c1_dragon = is_dragon_like
        # 趋势确认：收盘 > MA5 且 MA5 斜率向上
        c2_trend = (df["close"] > df["ma_short"]) & (df["ma_short_slope"] > 0)
        # 量能确认：近5日均量 > 20日均量
        c3_volume = df["vol_ratio"] > vol_expand_min
        # 回踩低吸：今日非涨停，在MA5附近
        c4_pullback = (df["daily_ret"] < 0.07) & (
            (df["close"] - df["ma_short"]).abs() / df["ma_short"].replace(0, np.nan) < pullback_max
        )

        buy_cond = c1_dragon & c2_trend & c3_volume & c4_pullback

        # ── 卖出条件（持仓后才生效，避免无持仓时刷卖出信号）──
        # 龙头破位：收盘 < MA5 且 MA5 斜率向下；量能枯竭：近5日均量 < 20日均量 × 0.7
        # 2026-09-03 修复（评审 P3）：原逻辑无条件产生卖出（880 信号中 848 个是卖出，
        # 无持仓也触发）。现在只在买入后的持仓期间允许这些卖出信号，与移动止损语义一致。
        c_sell_break = (df["close"] < df["ma_short"]) & (df["ma_short_slope"] < 0)
        c_sell_vol = df["vol_ratio"] < vol_shrink_exit
        sell_cond_raw = c_sell_break | c_sell_vol

        # 持仓感知：仅当该日处于买入信号之后的持仓区间才允许卖出
        # 顺序状态机（正确语义：买入开仓 → 卖出平仓 → 再买入再开仓）
        buy_arr = buy_cond.to_numpy(dtype=bool)
        sell_raw = sell_cond_raw.to_numpy(dtype=bool)
        in_pos = False
        valid_sell = np.zeros(len(df), dtype=bool)
        for i in range(len(df)):
            if buy_arr[i]:
                in_pos = True
            if sell_raw[i] and in_pos:
                valid_sell[i] = True
                in_pos = False
        sell_cond = sell_cond_raw & pd.Series(valid_sell, index=df.index)

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[sell_cond, "signal"] = -1

        # ── 置信度：龙头强度 + 量能强度 ────────────────
        dragon_strength = (
            consecutive_strong.astype(float) * 0.4
            + is_strong_day.astype(float) * 0.3
            + has_strong_day_10d.astype(float) * 0.3
        ).clip(0, 1)
        vol_strength = df["vol_ratio"].clip(0.5, 2).fillna(1) / 2
        df["confidence"] = 0.0
        df.loc[buy_cond, "confidence"] = (dragon_strength * 0.6 + vol_strength * 0.4).clip(0.3, 1)

        # 卖出置信度
        df.loc[sell_cond, "confidence"] = 0.5

        # ── 移动止损（替代时间止损）──────────────────
        if trailing_stop_pct > 0:
            df = self._apply_trailing_stop_exit(df, trailing_stop_pct)

        buy_count = int(np.sum(df["signal"] == 1))
        sell_count = int(np.sum(df["signal"] == -1))
        last_signal = int(df["signal"].iloc[-1]) if len(df) else 0
        last_signal_desc = {1: "买入", -1: "卖出", 0: "无"}.get(last_signal, "无")
        logger.info(
            f"dragon_head 信号统计: {buy_count} 买入, {sell_count} 卖出 | "
            f"强势日={int(is_strong_day.sum())} | 当日最新信号: {last_signal_desc}"
        )

        return df
