"""资金流跟随策略 — 基于板块资金强度 + 创新高回踩 + 放量确认。

核心观点（来自实战笔记）：
在板块行情中，应跟随资金强度，而非逆向买入滞涨股。

5 步复盘流程映射到策略过滤层：
1. 找当天成交额排名靠前的热点板块 → 个股层面用"近期强势涨幅"近似板块热度
2. 从热点板块中筛选均线多头排列的个股 → MA5 > MA10 > MA20 > MA60
3. 剔除明显放出历史天量、走势异常的股票 → 成交量 < N 日均量的 abnormal_volume_ratio 倍
4. 优先关注创新高、回踩不破均线的个股 → 近 N 日创历史新高且回踩不破 MA20
5. 等待缩量回踩后的放量确认 → 缩量回踩 K 日后当日放量（量比 ≥ volume_expand_ratio）

设计说明：
- 仅使用 OHLCV 数据，不调用外部资金流 API，保持与 SignalRouter 兼容性
- "资金强度"用成交量扩张 + 价格创新高近似（主力愿意消化上方套牢盘）
- 卖出信号：均线多头排列破坏（MA5 < MA10）或量价背离连续出现
- confirm_days=0：放量确认本身已是当日触发条件，无需二次确认
"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class MoneyFlowFollowStrategy(Strategy):
    """资金流跟随策略。

    P0 修复（基于全市场评估）：原 5 条件 AND 过严导致 250 天零信号。
    改为 3 核心条件 + 2 增强条件（boost 置信度而非硬阻断）：

    核心条件（必须全部满足，AND）：
    1. 均线多头排列：MA5 > MA10 > MA20 > MA60（趋势确认）
    2. 创新高回踩不破 MA20：近 N 日创新高后回踩 0-5%，且收盘 > MA20
    3. 放量确认：当日量比 ≥ 1.2（放宽原 1.5）

    增强条件（满足提升置信度，不满足不阻断）：
    4. 近期强势：10 日涨幅 ≥ 3%（热点近似）
    5. 缩量回踩：前 2 日量能递减（健康回踩）

    卖出条件（任一满足）：
    - 均线多头排列破坏：MA5 < MA10
    - 跌破 MA20（趋势破坏）
    """

    name = "money_flow_follow"

    def validate_params(self) -> None:
        ma_periods = self.params.get("ma_periods", [5, 10, 20, 60])
        if not isinstance(ma_periods, list) or len(ma_periods) < 4:
            raise ValueError("ma_periods 必须是长度 ≥ 4 的列表")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        ma_periods: list[int] = [int(x) for x in self.params.get("ma_periods", [5, 10, 20, 60])]
        new_high_window = int(self.params.get("new_high_window", 20))
        new_high_lookback = int(self.params.get("new_high_lookback", 60))  # P0: 120→60 放宽
        pullback_max_pct = float(self.params.get("pullback_max_pct", 0.08))  # P0: 0.05→0.08 放宽
        volume_shrink_days = int(self.params.get("volume_shrink_days", 2))
        volume_expand_ratio = float(self.params.get("volume_expand_ratio", 1.2))
        abnormal_volume_ratio = float(self.params.get("abnormal_volume_ratio", 3.0))
        recent_gain_window = int(self.params.get("recent_gain_window", 10))
        recent_gain_threshold = float(self.params.get("recent_gain_threshold", 0.03))
        vol_ma_window = int(self.params.get("vol_ma_window", 60))

        # ── 计算均线 ──
        ma5 = df["close"].rolling(ma_periods[0]).mean()
        ma10 = df["close"].rolling(ma_periods[1]).mean()
        ma20 = df["close"].rolling(ma_periods[2]).mean()
        ma60 = df["close"].rolling(ma_periods[3]).mean()

        df["ma5"] = ma5
        df["ma10"] = ma10
        df["ma20"] = ma20
        df["ma60"] = ma60

        # ── 条件1：近期强势涨幅（热点近似）──
        recent_gain = (df["close"] / df["close"].shift(recent_gain_window) - 1.0)
        cond_hot = recent_gain >= recent_gain_threshold

        # ── 条件2：均线多头排列 ──
        cond_ma_aligned = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)

        # ── 条件3：非历史天量 ──
        vol_ma = df["volume"].rolling(vol_ma_window).mean()
        # 当日量不超过 N 日均量的 abnormal_volume_ratio 倍（剔除异常放量）
        cond_not_abnormal = df["volume"] < vol_ma * abnormal_volume_ratio
        # vol_ma 为 NaN 的行（窗口期不足）放行
        cond_not_abnormal = cond_not_abnormal | vol_ma.isna()

        # ── 条件4：创新高回踩不破 MA20 ──
        # P0 修复：原逻辑 rolling_high 含当日，创新高当日即满足回踩条件（无回踩）
        # 修复：用 shift(1) 排除当日，要求"前 N 日已创新高"+"当日回踩（收盘低于前高）"
        rolling_high_prev = df["high"].rolling(new_high_window).max().shift(1)
        historical_high = df["high"].rolling(new_high_lookback).max().shift(1)
        # 创新高：前 N 日最高 = 历史最高（已创新高，非当日创新高）
        made_new_high = rolling_high_prev >= historical_high
        # 回踩：当日收盘低于前 N 日最高（真正回踩，非继续创新高）
        pullback_from_high = (df["close"] - rolling_high_prev) / rolling_high_prev
        cond_pullback_hold = made_new_high & (pullback_from_high < 0) & (pullback_from_high >= -pullback_max_pct)
        # 回踩不破 MA20
        cond_above_ma20 = df["close"] > ma20

        # ── 条件5：缩量回踩后放量确认 ──
        # 缩量回踩：前 volume_shrink_days 日成交量逐日递减（回踩缩量）
        vol_shrinking = pd.Series(True, index=df.index)
        for i in range(1, volume_shrink_days + 1):
            vol_shrinking = vol_shrinking & (df["volume"].shift(i) < df["volume"].shift(i + 1))
        # 当日放量：今日成交量 / 昨日成交量 ≥ volume_expand_ratio
        vol_ratio_today = df["volume"] / df["volume"].shift(1)
        cond_volume_confirm = vol_shrinking & (vol_ratio_today >= volume_expand_ratio)

        # ── 买入信号：3 核心条件 AND（P0 修复：原 5 AND 过严零信号）──
        buy_cond = (
            cond_ma_aligned          # 核心1: 均线多头排列
            & cond_pullback_hold    # 核心2: 创新高回踩不破 MA20
            & cond_above_ma20
            & cond_volume_confirm    # 核心3: 放量确认
        )

        # ── 卖出信号：均线多头排列破坏 或 跌破 MA20 ──
        ma_breakdown = (ma5 < ma10) & (ma5.shift(1) >= ma10.shift(1))  # MA5 下穿 MA10
        below_ma20 = df["close"] < ma20
        sell_cond = ma_breakdown | below_ma20

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        # 风控优先：卖出信号覆盖买入信号（趋势破坏时不入场）
        df.loc[sell_cond, "signal"] = -1

        # ── 置信度计算（P0 修复：3 核心保底 + 2 增强 + 1 反向惩罚）──
        # 基础置信度 0.6（3 核心条件已满足）
        base_confidence = pd.Series(0.6, index=df.index)
        # 增强条件1：近期强势 → +0.2
        hot_boost = pd.Series(0.0, index=df.index)
        hot_boost[cond_hot] = 0.2
        # 增强条件2：缩量回踩 → +0.1（健康回踩形态）
        shrink_boost = pd.Series(0.0, index=df.index)
        shrink_boost[vol_shrinking] = 0.1
        # 量比强度系数：放量越充分置信度越高（0.0-0.1）
        vol_strength_bonus = (vol_ratio_today / volume_expand_ratio).clip(0, 2.0) / 20.0
        # 反向惩罚：历史天量 → -0.3（天量常见于主力出货，从核心条件降级为反向增强）
        # 仍允许买入（不阻断），但显著降低置信度，使其易被 min_confidence 阈值过滤
        abnormal_penalty = pd.Series(0.0, index=df.index)
        abnormal_penalty[~cond_not_abnormal] = 0.3
        # 组合
        confidence = (
            base_confidence + hot_boost + shrink_boost + vol_strength_bonus - abnormal_penalty
        ).clip(0, 1.0).fillna(0.0)

        df["confidence"] = confidence
        # 无信号时置信度为 0
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        # 卖出信号置信度：跌破 MA20 = 0.8（趋势破坏），MA5 下穿 MA10 = 0.6（信号确认）
        sell_mask = df["signal"] == -1
        df.loc[sell_mask & below_ma20, "confidence"] = 0.8
        df.loc[sell_mask & ~below_ma20, "confidence"] = 0.6

        return df
