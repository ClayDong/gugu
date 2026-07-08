"""双阴钱袋子策略：主力底部洗盘结束、拉升启动的拐点确认信号。

来源：实战派「双阴钱袋子」战法（微信公众号文章深度整理）

第一性原理：
- 主力洗盘的完整动作闭环：缩阴是洗 → 倍量红是拉
- 散户割肉完毕 → 抛压耗尽 → 主力锁仓 → 倍量反转拉升
- 不是普通低吸，而是「洗盘结束拐点」+「倍量反转确认」双重信号

与 pullback_low 的根本差异：
- pullback_low: 当日缩量回踩即买入（单根K线）
- double_yin: 必须双阴洗盘 + 次日反转K线才买入（3根K线组合）

═══════════════════════════════════════════════════════════
标准双阴前置条件（4个硬AND，缺一不可）
═══════════════════════════════════════════════════════════
1. 趋势前提：MA20 持续向上拐头（ma_slope > 0）
2. K线结构：上涨中途连续两根调整阴线（shift2 阴 + shift1 阴）
3. 支撑不破：双阴最低点 ≥ MA20 × 0.97（精准回踩不破位）
4. 反转K线：当日收企稳十字（body<1%）或小阳（涨幅 0~3%）
   —— 即「钱袋子图标出现日」，入场点1（稳健仓）

═══════════════════════════════════════════════════════════
评分项（0-1 置信度，非硬性门槛）
═══════════════════════════════════════════════════════════
- 第二阴极致缩量（vol_ratio < 0.6）：+0.30（抛压耗尽的核心证据）
- 第一阴缩量（vol_ratio < 0.9）：+0.20（洗盘节奏正常）
- 当日量能回升（vol_ratio ≥ 1.0）：+0.20（主力开始介入）
- 当日收复昨日失地（close > yesterday close）：+0.20（反转力度）
- MA20 > MA60（中期趋势向上）：+0.10（趋势共振）

倍量反转加仓信号（次日，独立买入信号）：
- 昨日为钱袋子买入信号日
- 当日 2 倍量阳线 + close > 双阴最高点 → 额外买入信号（加仓）

═══════════════════════════════════════════════════════════
入场/止损/止盈实操纪律
═══════════════════════════════════════════════════════════
入场点1（稳健仓）：钱袋子图标出现当日尾盘低吸
入场点2（加仓仓）：信号次日放量突破双阴高点（2倍量阳线当天）加仓
止损：有效跌破 MA20 收盘直接离场（盘中刺破收回可持有）
止盈1：第一根放量大阳线冲高 5%-8%，减仓半仓
止盈2：连续阳线拉升出现放量长上影、高位巨量滞涨，清仓

设计说明：
- 原版 10 个 AND 条件同时满足概率极低（30股×500天=0 trades）
- 文章描述的两个入场点是「分两天」的，原代码错误地合并到同一天
- 现拆分为：核心4条件（钱袋子日）+ 评分制置信度 + 次日倍量加仓信号
- 去除了 3 个冗余避坑过滤（与前置条件取反等价，无信息增量）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class DoubleYinMoneyBagStrategy(Strategy):
    """双阴钱袋子策略：主力洗盘结束 + 反转K线确认。

    4 核心硬 AND 条件 + 5 评分项（置信度）+ 次日倍量加仓信号。
    与 pullback_low 互补：pullback_low 是单K低吸，本策略是3K组合拐点确认。
    """

    name = "double_yin"

    def validate_params(self) -> None:
        """校验参数语义。"""
        if self.params.get("ma_trend_window", 20) >= self.params.get("ma_long_window", 60):
            raise ValueError("ma_trend_window 必须小于 ma_long_window")
        vol_ratio_min = float(self.params.get("second_yin_vol_ratio_max", 0.6))
        vol_ratio_max = float(self.params.get("first_yin_vol_ratio_max", 0.9))
        if vol_ratio_min >= vol_ratio_max:
            raise ValueError("second_yin_vol_ratio_max 必须小于 first_yin_vol_ratio_max")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成双阴钱袋子信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        ma_trend_window = int(self.params.get("ma_trend_window", 20))
        ma_long_window = int(self.params.get("ma_long_window", 60))
        ma_slope_window = int(self.params.get("ma_slope_window", 5))
        _first_yin_vol_max = float(self.params.get("first_yin_vol_ratio_max", 0.9))
        _second_yin_vol_max = float(self.params.get("second_yin_vol_ratio_max", 0.6))
        breakout_vol_min = float(self.params.get("breakout_vol_ratio_min", 2.0))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.0))  # 跌破MA20即止损
        take_profit_pct = float(self.params.get("take_profit_pct", 0.08))
        max_upper_shadow_pct = float(self.params.get("max_upper_shadow_pct", 0.02))
        stagnation_vol_ratio = float(self.params.get("stagnation_vol_ratio", 2.5))
        # 反转K线参数
        doji_body_max = float(self.params.get("doji_body_max", 0.01))
        small_yang_max_gain = float(self.params.get("small_yang_max_gain", 0.03))

        # ── 计算均线 ──────────────────────────────────
        df["ma_trend"] = df["close"].rolling(ma_trend_window).mean()
        df["ma_long"] = df["close"].rolling(ma_long_window).mean()

        # MA20 斜率（持续向上拐头判定）
        df["ma_slope"] = df["ma_trend"].diff(ma_slope_window) / df["ma_trend"].shift(ma_slope_window)

        # ── 计算量能指标 ──────────────────────────────
        df["vol_ma5"] = df["volume"].rolling(5).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # ── K线基础属性 ────────────────────────────────
        df["daily_return"] = df["close"].pct_change()
        df["is_yin"] = (df["close"] < df["open"]).astype(bool)
        df["is_yang"] = (df["close"] > df["open"]).astype(bool)
        df["body"] = (df["close"] - df["open"]).abs() / df["open"].replace(0, np.nan)
        df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["open"].replace(0, np.nan)

        # ════════════════════════════════════════════════
        # 核心4硬AND条件（钱袋子图标日）
        # ════════════════════════════════════════════════
        # 条件1: 趋势前提（MA20向上拐头 + 股价在MA20上方）
        cond_trend_up = (df["ma_slope"] > 0) & (df["close"] > df["ma_trend"])

        # 条件2: 双阴结构（shift2 + shift1 均为阴线）
        df["yin_2d_ago"] = df["is_yin"].shift(2)
        df["yin_1d_ago"] = df["is_yin"].shift(1)
        cond_double_yin = df["yin_2d_ago"] & df["yin_1d_ago"]

        # 条件3: 支撑不破（双阴最低点 ≥ MA20 × 0.97）
        df["yin_low"] = np.minimum(df["low"].shift(2), df["low"].shift(1))
        df["ma20_ref"] = df["ma_trend"].shift(1)
        cond_ma20_support = df["yin_low"] >= df["ma20_ref"] * (1 - 0.03)

        # 条件4: 反转K线（当日十字 or 小阳）
        df["is_doji"] = df["body"] < doji_body_max
        df["is_small_yang"] = df["is_yang"] & (df["daily_return"] <= small_yang_max_gain) & (df["daily_return"] >= 0)
        cond_kline_reversal = df["is_doji"] | df["is_small_yang"]

        # 核心买入信号（4硬AND）
        core_buy_cond = cond_trend_up & cond_double_yin & cond_ma20_support & cond_kline_reversal

        # ════════════════════════════════════════════════
        # 评分项（置信度，0-1）
        # ════════════════════════════════════════════════
        df["first_yin_vol_ratio"] = df["vol_ratio"].shift(2)
        df["second_yin_vol_ratio"] = df["vol_ratio"].shift(1)
        df["ma20_above_ma60"] = df["ma_trend"] > df["ma_long"]

        score_second_shrink = (1 - df["second_yin_vol_ratio"].clip(0, 1)).clip(0, 1) * 0.30
        score_first_shrink = (1 - df["first_yin_vol_ratio"].clip(0, 1)).clip(0, 1) * 0.20
        score_today_vol = (df["vol_ratio"].clip(0, 2) / 2).clip(0, 1) * 0.20
        score_recovers = (df["close"] > df["close"].shift(1)).astype(float) * 0.20
        score_trend_resonance = df["ma20_above_ma60"].astype(float) * 0.10

        df["confidence_score"] = (
            score_second_shrink + score_first_shrink + score_today_vol
            + score_recovers + score_trend_resonance
        ).clip(0, 1)

        # ════════════════════════════════════════════════
        # 次日倍量反转加仓信号（独立买入信号）
        # ════════════════════════════════════════════════
        # 昨日为核心信号日 + 今日 2倍量阳线 + 收复双阴失地
        df["prev_buy_signal"] = core_buy_cond.shift(1).fillna(False)
        df["double_yin_high"] = np.maximum(df["high"].shift(2), df["high"].shift(1))
        cond_volume_breakout = df["vol_ratio"] >= breakout_vol_min
        cond_recovers_double_yin = df["close"] > df["double_yin_high"]
        add_position_cond = (
            df["prev_buy_signal"] & df["is_yang"]
            & cond_volume_breakout & cond_recovers_double_yin
        )

        # ════════════════════════════════════════════════
        # 买入信号：高置信度核心信号 OR 倍量加仓信号
        # ════════════════════════════════════════════════
        # 置信度阈值过滤低质量信号（4条件版本770笔→高置信度子集）
        min_confidence = float(self.params.get("min_confidence", 0.55))
        buy_cond = (core_buy_cond & (df["confidence_score"] >= min_confidence)) | add_position_cond

        # ════════════════════════════════════════════════
        # 卖出条件（止损 + 止盈分层）
        # ════════════════════════════════════════════════
        # 止损：跌破 MA20 × (1 - stop_loss_pct) 收盘离场
        df["stop_loss_price"] = df["ma_trend"] * (1 - stop_loss_pct)
        cond_stop_loss = df["close"] < df["stop_loss_price"]

        # 止盈1：第一根放量大阳线冲高 5%-8%（take_profit_pct × 0.6 触发）
        df["big_yang"] = (
            df["is_yang"]
            & (df["daily_return"] >= take_profit_pct * 0.6)
            & (df["vol_ratio"] >= 1.5)
        )
        cond_take_profit_1 = df["big_yang"]

        # 止盈2：高位巨量滞涨（放量 + 长上影 + 涨幅小）→ 清仓
        df["stagnation"] = (
            (df["vol_ratio"] >= stagnation_vol_ratio)
            & (df["upper_shadow"] > max_upper_shadow_pct)
            & (df["daily_return"].abs() < 0.02)
        )
        cond_take_profit_2 = df["stagnation"]

        # ════════════════════════════════════════════════
        # 信号赋值（卖出优先于买入）
        # ════════════════════════════════════════════════
        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        # 卖出信号优先（止损 + 止盈 + 巨量滞涨）
        df.loc[cond_stop_loss | cond_take_profit_1 | cond_take_profit_2, "signal"] = -1

        # 置信度：核心信号用评分，倍量加仓日置信度更高
        df["confidence"] = 0.0
        core_with_conf = core_buy_cond & (df["confidence_score"] >= min_confidence)
        df.loc[core_with_conf, "confidence"] = df.loc[core_with_conf, "confidence_score"].clip(0.5, 0.85)
        # 倍量加仓日置信度更高（已有前日信号确认）
        df.loc[add_position_cond, "confidence"] = 0.90

        return df
