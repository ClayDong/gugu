"""突破策略：达尔文箱体、Dual Thrust、支撑阻力。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()


class BoxBreakoutStrategy(Strategy):
    """达尔文箱体理论：箱体震荡突破为信号（v2 优化版）。

    识别近期价格箱体（最高/最低），突破箱体上沿买入，跌破下沿卖出。
    可选成交量确认（仅买入，卖出不需要量能确认）。

    v2 优化（对抗性审查修复）：
    1. 卖出信号不再要求量能确认 — 修复阴跌无法止损的致命 bug
       原逻辑：卖出也需要 volume > vol_ma * 1.5，但阴跌时量能萎缩，
       卖出信号永远不触发，导致持仓一路亏损无法退出
    2. 新增趋势过滤：MA20 > MA60 时才允许买入 — 过滤下跌趋势中的假突破
       第一性原理：箱体突破在上升趋势中有效（顺势追涨），
       在下降趋势中是假突破陷阱（逆势抄底）
    3. 新增最大持仓天数：超时强制卖出 — 防止资金被横盘股套牢
       突破后若 max_hold_days 内未触发卖出信号，说明趋势未如期发展，
       应退出等待下一个机会
    """

    name = "box_breakout"
    category = "breakout"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        w = int(self.params["box_window"])
        vol_confirm = bool(self.params.get("volume_confirm", True))
        vol_ratio = float(self.params.get("volume_ratio", 1.5))
        # v2 新增参数
        trend_filter = bool(self.params.get("trend_filter", True))
        ma_short = int(self.params.get("trend_ma_short", 20))
        ma_long = int(self.params.get("trend_ma_long", 60))
        max_hold_days = int(self.params.get("max_hold_days", 30))

        df["box_high"] = df["high"].rolling(w).max().shift(1)
        df["box_low"] = df["low"].rolling(w).min().shift(1)
        df["vol_ma"] = df["volume"].rolling(w).mean().shift(1)

        # v2: 趋势过滤均线
        if trend_filter:
            df["ma_short"] = df["close"].rolling(ma_short).mean()
            df["ma_long"] = df["close"].rolling(ma_long).mean()

        df["signal"] = 0

        # 突破箱体上沿买入
        breakout_up = df["close"] > df["box_high"]
        if vol_confirm:
            vol_ok = df["vol_ma"].notna() & (df["volume"] > df["vol_ma"] * vol_ratio)
            # vol_ma 为 NaN 的行（开箱期）跳过成交量确认，仅用价格突破判断
            vol_ok = vol_ok | df["vol_ma"].isna()
            breakout_up = breakout_up & vol_ok
        # v2: 趋势过滤 — 仅在 MA20 > MA60（上升趋势）时允许买入
        if trend_filter:
            trend_ok = df["ma_short"] > df["ma_long"]
            breakout_up = breakout_up & trend_ok
        df.loc[breakout_up, "signal"] = 1

        # 跌破箱体下沿卖出（v2 修复：不再要求量能确认）
        # 第一性原理：卖出是风控动作，不应依赖量能配合
        # 阴跌时量能萎缩，若要求量能确认则永远无法退出
        breakout_down = df["close"] < df["box_low"]
        df.loc[breakout_down, "signal"] = -1

        # v2: 最大持仓天数强制卖出
        # 突破后 max_hold_days 内未触发卖出信号 → 趋势未如期发展，退出
        if max_hold_days > 0:
            signal = df["signal"].to_numpy(dtype=int, copy=True)
            n = len(signal)
            i = 0
            while i < n:
                if signal[i] == 1:
                    exit_idx = min(i + max_hold_days, n - 1)
                    # 检查 (i, exit_idx] 是否已有卖出信号
                    has_sell = False
                    for j in range(i + 1, exit_idx + 1):
                        if signal[j] == -1:
                            has_sell = True
                            break
                    # 仅当 exit_idx 无买入信号时才强制卖出（避免覆盖买入）
                    if not has_sell and exit_idx > i and signal[exit_idx] != 1:
                        signal[exit_idx] = -1
                    i = exit_idx + 1
                else:
                    i += 1
            df["signal"] = signal

        # 置信度：突破幅度 / ATR(15)，避免 close 同时做信号和置信度分母
        atr_val = self._atr(df, int(self.params.get("box_window", 15)))
        atr_safe = atr_val.clip(lower=df["close"] * 0.01)
        df["confidence"] = (
            (df["close"] - df["box_high"]).clip(lower=0) / atr_safe
            + (df["box_low"] - df["close"]).clip(lower=0) / atr_safe
        ).clip(0, 1)
        # 无信号时置信度为 0
        df.loc[df["signal"] == 0, "confidence"] = 0.0

        buy_count = int(np.sum(df["signal"] == 1))
        sell_count = int(np.sum(df["signal"] == -1))
        logger.info(
            f"box_breakout v2 生成信号: {buy_count} 买入, {sell_count} 卖出, "
            f"趋势过滤={'on' if trend_filter else 'off'}, "
            f"最大持仓={max_hold_days}天"
        )

        return df


class DualThrustStrategy(Strategy):
    """Dual Thrust 突破策略。

    基于 N 日内的最高价-最低价、最高价-收盘价、收盘价-最低价计算上下轨。

    设计说明——跳过 confirm_days 确认机制：
    本策略直接重写 ``generate_signals()`` 而非实现 ``_generate_signals_impl()``，
    因此不走基类的 ``_require_confirmation()`` 模板方法。
    原因：Dual Thrust 的突破信号本身就是触发条件（收盘价穿越上下轨），
    信号当天即成立，无需后续 K 线二次确认；加入 confirm_days 反而会延迟入场、
    错过突破行情。这与均线突破策略（BreakoutStrategy）不同——后者依赖趋势延续，
    需要 confirm_days=1 确认非假突破。

    置信度归一化：
    confidence = 突破幅度 / range_val，经 ``.clip(0, 1)`` 归一化到 [0, 1]。
    无信号时 confidence=0。
    """

    name = "dual_thrust"
    category = "breakout"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        n = int(self.params["lookback"])
        k_up = float(self.params["k_up"])
        k_down = float(self.params["k_down"])

        hh = df["high"].rolling(n).max().shift(1)
        hc = df["close"].rolling(n).max().shift(1)
        lc = df["close"].rolling(n).min().shift(1)
        ll = df["low"].rolling(n).min().shift(1)

        range_val = np.maximum(hh - lc, hc - ll)
        df["upper"] = df["open"] + k_up * range_val
        df["lower"] = df["open"] - k_down * range_val

        df["signal"] = 0
        df.loc[df["close"] > df["upper"], "signal"] = 1
        df.loc[df["close"] < df["lower"], "signal"] = -1

        df["confidence"] = (
            ((df["close"] - df["upper"]).clip(lower=0) / range_val.shift(1).replace(0, 1))
            + ((df["lower"] - df["close"]).clip(lower=0) / range_val.shift(1).replace(0, 1))
        ).clip(0, 1)
        # 无信号时置信度为 0
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        return df
