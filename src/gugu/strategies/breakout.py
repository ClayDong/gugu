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
        # trend_mode: "crossover"（MA短 > MA长，滞后大但稳定）
        #            "slope"（MA斜率正，响应快但噪声多，默认）
        trend_mode = str(self.params.get("trend_mode", "slope"))
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
        # v2: 趋势过滤 — 从硬 AND 改为置信度调整（2026-09-02 修复）
        # 原因：三重 AND 锁死信号（突破+量+趋势向上），在震荡市几乎不触发
        # 修复：趋势向上=高置信度，趋势向下=低置信度（仍保留信号）
        if trend_filter:
            if trend_mode == "slope":
                ma = df["close"].rolling(ma_short).mean()
                ma_slope = (ma - ma.shift(5)) / ma.shift(5).replace(0, 1e-10)
                trend_ok = ma_slope > 0
            else:  # crossover — MA短 > MA长
                df["ma_short"] = df["close"].rolling(ma_short).mean()
                df["ma_long"] = df["close"].rolling(ma_long).mean()
                trend_ok = df["ma_short"] > df["ma_long"]
            # 趋势不做硬过滤，只影响置信度（见下方 confidence 计算）
            df["_trend_ok"] = trend_ok.astype(float)
        df.loc[breakout_up, "signal"] = 1

        # 跌破箱体下沿卖出（v2 修复：不再要求量能确认）
        # 第一性原理：卖出是风控动作，不应依赖量能配合
        # 阴跌时量能萎缩，若要求量能确认则永远无法退出
        # 2026-09-03 修复（评审 P3）：持仓感知——只在买入后的持仓区间允许卖出，
        # 无持仓时不再刷卖出信号（213 卖出中大量为无持仓触发）
        breakout_down = df["close"] < df["box_low"]
        buy_arr = breakout_up.to_numpy(dtype=bool)
        sell_raw_arr = breakout_down.to_numpy(dtype=bool)
        in_pos = False
        valid_sell = np.zeros(len(df), dtype=bool)
        for i in range(len(df)):
            if buy_arr[i]:
                in_pos = True
            if sell_raw_arr[i] and in_pos:
                valid_sell[i] = True
                in_pos = False
        df.loc[breakout_down & pd.Series(valid_sell, index=df.index), "signal"] = -1

        # v2: 最大持仓天数强制卖出（抽取为基类公共方法）
        df = self._apply_max_hold_exit(df, max_hold_days=max_hold_days)

        # 置信度：根据信号方向分别计算
        # 修复：买入用突破幅度/ATR，卖出用跌破幅度/ATR，避免两者同时非零时叠加
        atr_val = self._atr(df, int(self.params.get("box_window", 15)))
        atr_safe = atr_val.clip(lower=df["close"] * 0.01)
        buy_conf = ((df["close"] - df["box_high"]) / atr_safe).clip(lower=0).clip(upper=1)
        sell_conf = ((df["box_low"] - df["close"]) / atr_safe).clip(lower=0).clip(upper=1)
        # 趋势调整：趋势向上时买入置信度不打折，趋势向下时打 5 折
        if trend_filter and "_trend_ok" in df.columns:
            trend_weight = df["_trend_ok"].fillna(0.5).clip(0.5, 1.0)
            buy_conf = buy_conf * trend_weight
        df["confidence"] = 0.0
        df.loc[df["signal"] == 1, "confidence"] = buy_conf
        df.loc[df["signal"] == -1, "confidence"] = sell_conf

        buy_count = int(np.sum(df["signal"] == 1))
        sell_count = int(np.sum(df["signal"] == -1))
        # P1-2 修复（2026-08-05）：日志口径——"N买入M卖出"是 120 天历史窗口
        # 累计信号数，不是当日信号。用户看到"4 买入"误以为今天有 4 个买入，
        # 实为 router 只取最后一行（当日）。必须明确标注统计范围：
        #   - 历史窗口（120天累计）：诊断策略活跃度用
        #   - 当日最新（最后一行）：真实信号语义
        last_signal = int(df["signal"].iloc[-1]) if len(df) else 0
        last_date = str(df["date"].iloc[-1])[:10] if "date" in df.columns and len(df) else "?"
        last_signal_desc = {1: "买入", -1: "卖出", 0: "无"}.get(last_signal, "无")
        logger.info(
            f"box_breakout v2 信号统计[120天窗口]: {buy_count} 买入, {sell_count} 卖出, "
            f"趋势过滤={'on' if trend_filter else 'off'}, 最大持仓={max_hold_days}天 | "
            f"当日({last_date})最新信号: {last_signal_desc}"
        )

        return df
