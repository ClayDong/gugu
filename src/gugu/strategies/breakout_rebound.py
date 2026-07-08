"""断板反包策略：涨停板断裂后主力资金反手包回的短线拐点信号。

来源：A股实战派「断板反包」战法（短线主力行为学深度整理）

第一性原理：
- 涨停板 = 主力资金强势介入的明确标志（散户无力封板）
- 断板 = 涨停次日未能继续封板，主力借机洗盘（洗出短线获利盘）
- 反包 = 主力资金重新介入，收盘价突破断板日高点，确认做多意愿未改
- 完整闭环：封板（介入）→ 断板（洗盘）→ 反包（再介入），主力行为可追溯

与 double_yin 的根本差异：
- double_yin: 上涨中途双阴洗盘 + 倍量反转（3K组合，趋势中继）
- breakout_rebound: 涨停板后断板 + 反包突破（涨停+断板+反包，短线主力行为）
- 互补关系：double_yin 适合波段趋势，breakout_rebound 适合短线强势股

═══════════════════════════════════════════════════════════
核心硬条件（5个硬AND，缺一不可）v0.5.8 调整
═══════════════════════════════════════════════════════════
1. 涨停前提：前 N 日内（默认5日）出现涨停板（涨幅 ≥ limit_up_pct）
2. 断板结构：涨停次日未继续涨停（涨幅 < limit_up_pct），出现调整
3. 反包突破：当日收盘价 > 断板日最高价（包回失地）
4. 阳线收复：当日为阳线（close > open，反包语义要求）
5. 趋势过滤：MA20 向上 + 收盘价在 MA20 上方（中期趋势支撑）

注：v0.5.7 原条件4"量能确认（阳线+放量）"过于严格导致买入信号过少（15个），
v0.5.8 拆分为：阳线保留为硬AND（语义合理），量比降级为评分项强化（+0.30）。

═══════════════════════════════════════════════════════════
评分项（0-1 置信度，非硬性门槛）
═══════════════════════════════════════════════════════════
- 断板日缩量（vol_ratio < 0.8）：+0.30（洗盘充分的核心证据）
- 反包日量能强度（vol_ratio/3 clip 0-1）：+0.20（主力介入力度）
- 反包力度强（close > 涨停日最高价）：+0.20（强势反包）
- MA20 斜率向上（ma_slope > 0）：+0.15（趋势共振）
- 距涨停日天数 1-3 日（days_since_limit_up ∈ [1,3]）：+0.15（时效性）

═══════════════════════════════════════════════════════════
入场/止损/止盈实操纪律（v0.5.8 持仓状态判断）
═══════════════════════════════════════════════════════════
入场点：反包确认当日尾盘买入（收盘价突破断板日最高价）
止损1：持仓中，跌破入场点 low × 0.97（反包失效）
止损2：持仓中，跌破 MA20 收盘离场（趋势破坏）
止盈1：持仓中，涨幅达 4.8%+ 放量减半仓（短线目标）
止盈2：持仓中，高位巨量滞涨清仓（主力出货信号）
时间止损：持仓超 5 天 + 涨幅 < 3% 减仓（无进展离场）

注：v0.5.8 引入 in_position（持仓状态）判断，所有卖出条件仅在
持仓窗口（买入后 time_stop_window 天）内生效，避免非持仓日子
产生卖出信号导致泛滥（v0.5.7 根因：2640卖出 vs 15买入）。

设计说明：
- 涨停板识别使用涨幅 ≥ 9.5%（A股10%涨停板限制，留0.5%容差应对数据精度）
- 断板日不限制阴阳（重点是未继续涨停），但缩量断板评分更高
- 反包必须突破断板日最高价，突破涨停日最高价评分更高（强反包）
- 与 box_breakout 区别：box_breakout 是箱体突破，本策略是涨停后行为分析
- 依赖 MarketFilter 在熊市拦截（熊市主力可能伪装反包出货）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class BreakoutReboundStrategy(Strategy):
    """断板反包策略：涨停板断裂后主力反包介入的短线信号。

    5 核心硬 AND 条件 + 5 评分项（置信度）。
    与 double_yin 互补：double_yin 是波段拐点，本策略是短线主力行为。
    """

    name = "breakout_rebound"

    def validate_params(self) -> None:
        """校验参数语义。"""
        ma_trend = int(self.params.get("ma_trend_window", 20))
        ma_long = int(self.params.get("ma_long_window", 60))
        if ma_trend >= ma_long:
            raise ValueError("ma_trend_window 必须小于 ma_long_window")
        lookback = int(self.params.get("limit_up_lookback", 5))
        if lookback < 1:
            raise ValueError("limit_up_lookback 必须大于 0")
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        if not 0.05 <= limit_up_pct <= 0.11:
            raise ValueError("limit_up_pct 应在 5%-11% 之间")
        breakout_vol = float(self.params.get("breakout_vol_ratio", 1.5))
        if breakout_vol < 1.0:
            raise ValueError("breakout_vol_ratio 必须 >= 1.0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成断板反包信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        ma_trend_window = int(self.params.get("ma_trend_window", 20))
        ma_long_window = int(self.params.get("ma_long_window", 60))
        ma_slope_window = int(self.params.get("ma_slope_window", 5))
        limit_up_pct = float(self.params.get("limit_up_pct", 0.095))
        limit_up_lookback = int(self.params.get("limit_up_lookback", 5))
        breakout_vol_ratio = float(self.params.get("breakout_vol_ratio", 1.5))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.03))
        take_profit_pct = float(self.params.get("take_profit_pct", 0.08))
        max_upper_shadow_pct = float(self.params.get("max_upper_shadow_pct", 0.02))
        stagnation_vol_ratio = float(self.params.get("stagnation_vol_ratio", 2.5))
        time_stop_window = int(self.params.get("time_stop_window", 5))
        time_stop_threshold = float(self.params.get("time_stop_threshold", 0.03))

        # ── 计算均线 ──────────────────────────────────
        df["ma_trend"] = df["close"].rolling(ma_trend_window).mean()
        df["ma_long"] = df["close"].rolling(ma_long_window).mean()
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
        # 条件1: 涨停板识别（前 N 日内有涨停）
        # ════════════════════════════════════════════════
        # 用 np.round 消除浮点精度（1.095 倍的 pct_change 可能 = 0.094999...）
        df["is_limit_up"] = np.round(df["daily_return"], 4) >= limit_up_pct
        # 在 lookback 窗口内是否有涨停（排除当日，当日涨停不算"前N日"）
        df["has_limit_up_before"] = (
            df["is_limit_up"].rolling(window=limit_up_lookback).sum().shift(1) > 0
        )

        # 找到最近一次涨停日的位置（用于计算断板和反包）
        # limit_up_day_offset: 距最近涨停日的天数（1表示昨天涨停）
        df["limit_up_day_offset"] = np.nan
        limit_up_indices = df.index[df["is_limit_up"]].tolist()
        for i in range(len(df)):
            # 找到 i 之前最近的涨停日
            prev_limit_ups = [idx for idx in limit_up_indices if idx < i]
            if prev_limit_ups:
                last_lu = prev_limit_ups[-1]
                offset = i - last_lu
                if offset <= limit_up_lookback:
                    df.loc[df.index[i], "limit_up_day_offset"] = offset

        # ════════════════════════════════════════════════
        # 条件2: 断板结构（涨停次日未继续涨停）
        # ════════════════════════════════════════════════
        # 涨停次日 = limit_up_day_offset == 1 的那天
        # 断板 = 涨停次日的涨幅 < limit_up_pct（未继续涨停）
        # 我们需要识别"断板日"，即涨停后的调整日
        df["is_break_day"] = (
            (df["limit_up_day_offset"] == 1)
            & (df["daily_return"] < limit_up_pct)
        )

        # 找到断板日的高点（用于反包判定）
        # 断板日 = is_break_day == True 的那天
        # 我们需要追踪最近一次断板日的最高价
        df["break_day_high"] = np.nan
        df["break_day_low"] = np.nan
        df["break_day_vol_ratio"] = np.nan
        df["limit_up_day_high"] = np.nan
        break_indices = df.index[df["is_break_day"]].tolist()
        for i in range(len(df)):
            # 找到 i 之前最近的断板日
            prev_breaks = [idx for idx in break_indices if idx < i]
            if prev_breaks:
                last_break = prev_breaks[-1]
                # 断板日必须在 lookback 窗口内
                if i - last_break <= limit_up_lookback:
                    df.loc[df.index[i], "break_day_high"] = df.loc[df.index[last_break], "high"]
                    df.loc[df.index[i], "break_day_low"] = df.loc[df.index[last_break], "low"]
                    df.loc[df.index[i], "break_day_vol_ratio"] = df.loc[df.index[last_break], "vol_ratio"]

                    # 找到对应的涨停日（断板日的前一天）
                    lu_idx = last_break - 1
                    if lu_idx >= 0 and df.loc[df.index[lu_idx], "is_limit_up"]:
                        df.loc[df.index[i], "limit_up_day_high"] = df.loc[df.index[lu_idx], "high"]

        # ════════════════════════════════════════════════
        # 条件3: 反包突破（收盘价 > 断板日最高价）
        # ════════════════════════════════════════════════
        cond_rebound = df["close"] > df["break_day_high"]

        # ════════════════════════════════════════════════
        # 条件4: 量能确认（当日阳线 + 放量）→ v0.5.8 拆分
        # ════════════════════════════════════════════════
        # 原 5 硬AND 同时满足概率极低（15买入/2640卖出），v0.5.8 拆分：
        #   - is_yang（阳线）保留为硬AND：反包语义要求阳线收复
        #   - vol_ratio >= breakout_vol_ratio 降级为评分项强化
        # 保留 5 个核心硬AND：
        #   1. 涨停前提（has_limit_up_before）
        #   2. 断板结构（break_day_high 非空）
        #   3. 反包突破（cond_rebound）
        #   4. 阳线收复（is_yang）
        #   5. 趋势过滤（cond_trend）
        # 量比通过 score_rebound_vol_strong 评分项强化（放量 → +0.30）
        cond_volume_confirm = df["is_yang"] & (df["vol_ratio"] >= breakout_vol_ratio)

        # ════════════════════════════════════════════════
        # 条件5: 趋势过滤（MA20向上 + 收盘价在MA20上方）
        # ════════════════════════════════════════════════
        cond_trend = (df["ma_slope"] > 0) & (df["close"] > df["ma_trend"])

        # ════════════════════════════════════════════════
        # 核心买入信号（5硬AND，量比降级为评分项）
        # ════════════════════════════════════════════════
        # has_limit_up_before 已包含在 limit_up_day_offset 计算中
        # break_day_high 非空意味着存在断板日
        # is_yang 保留为硬AND（反包必须是阳线收复）
        core_buy_cond = (
            df["has_limit_up_before"]
            & cond_rebound
            & df["is_yang"]
            & cond_trend
            & df["break_day_high"].notna()
        )

        # ════════════════════════════════════════════════
        # 评分项（置信度，0-1）v0.5.8 调整：
        #   量能确认从硬AND降级为评分项，权重提升至 0.30
        #   断板缩量保持 0.25
        #   强反包保持 0.20
        #   趋势共振保持 0.15
        #   时效性保持 0.10
        # ════════════════════════════════════════════════
        # 1. 断板日缩量（洗盘充分）
        score_break_shrink = (1 - df["break_day_vol_ratio"].clip(0, 1)).clip(0, 1) * 0.25

        # 2. 反包日量能强化（阳线+放量 → +0.30，原硬门槛降级为评分项）
        #    阳线且量比达标得满分，否则按 vol_ratio/3 比例折算
        score_rebound_vol_strong = np.where(
            cond_volume_confirm,
            0.30,
            (df["vol_ratio"].clip(0, 3) / 3 * 0.30).clip(0, 0.30),
        )

        # 3. 反包力度（突破涨停日最高价 = 强反包）
        score_strong_rebound = (df["close"] > df["limit_up_day_high"]).astype(float) * 0.20

        # 4. MA20 斜率向上
        score_trend_up = (df["ma_slope"] > 0).astype(float) * 0.15

        # 5. 距涨停日天数 1-3 日最佳（时效性）
        # offset=1(昨天涨停今天断板明天反包) 或 offset=2(前天涨停昨天断板今天反包)
        # 注意：limit_up_day_offset 是"距最近涨停日"，但反包日是断板日的次日
        # 所以反包日的 offset = 断板日的 offset + 1 = 2（昨天涨停→前天）
        # 实际上 break_day 的 offset=1，反包日 break_day+1 的 offset=2
        score_timing = ((df["limit_up_day_offset"] >= 2) & (df["limit_up_day_offset"] <= 4)).astype(float) * 0.10

        df["confidence_score"] = (
            score_break_shrink + score_rebound_vol_strong + score_strong_rebound
            + score_trend_up + score_timing
        ).clip(0, 1)

        # ════════════════════════════════════════════════
        # 卖出条件（v0.5.8 重构：持仓状态判断 + 分层止损止盈）
        # ════════════════════════════════════════════════
        # v0.5.7 根因诊断：
        #   1. close < ma_trend 过于敏感 → 2640 卖出 vs 15 买入
        #   2. 持仓22天亏损 vs 14天盈利 → 缺少时间止损
        # v0.5.8 重构：
        #   1. 引入 in_position（持仓状态）：近 time_stop_window 天有买入信号
        #   2. 所有卖出条件只在持仓状态生效，避免对所有日子产生卖出信号
        #   3. 止损1：跌破入场点 low × (1 - stop_loss_pct)
        #   4. 止损2：持仓中跌破 MA20（恢复原阈值，因仅在持仓状态判断）
        #   5. 止盈1：涨幅 ≥ take_profit_pct * 0.6（恢复原阈值）
        #   6. 时间止损：持仓超 time_stop_window 天 + 涨幅 < time_stop_threshold

        # ── 持仓状态判断 ──────────────────────────────
        # 计算 buy_cond_final（在 in_position 之前）
        min_confidence = float(self.params.get("min_confidence", 0.0))
        buy_cond_final = core_buy_cond & (df["confidence_score"] >= min_confidence)

        # 持仓窗口 = time_stop_window * 4（确保持仓期间一直 in_position=True）
        # v0.5.8 修复：原 in_position 窗口 = time_stop_window，导致持仓5天后
        # in_position=False，时间止损和其他卖出条件失效
        position_window = time_stop_window * 4
        df["in_position"] = buy_cond_final.rolling(window=position_window).sum() > 0

        # ── 追踪入场点信息（用于止损1和时间止损） ──────
        # entry_low：买入信号日的 low，forward fill 到持仓期间
        # entry_close：买入信号日的 close，用于计算持仓收益率
        df["entry_low"] = np.nan
        df["entry_close"] = np.nan
        df.loc[buy_cond_final, "entry_low"] = df.loc[buy_cond_final, "low"]
        df.loc[buy_cond_final, "entry_close"] = df.loc[buy_cond_final, "close"]
        df["entry_low"] = df["entry_low"].ffill()
        df["entry_close"] = df["entry_close"].ffill()

        # ── 止损1：跌破入场点 low × (1 - stop_loss_pct) ──
        cond_stop_loss_1 = df["in_position"] & (df["close"] < df["entry_low"] * (1 - stop_loss_pct))

        # ── 止损2：持仓中跌破 MA20（趋势破坏） ──
        # v0.5.8 恢复原阈值 close < ma_trend，因仅在持仓状态判断
        # 非持仓日子不产生卖出信号，避免泛滥
        cond_stop_loss_2 = df["in_position"] & (df["close"] < df["ma_trend"])

        # ── 止盈1：涨幅达 take_profit_pct * 0.6（大阳线冲高） ──
        # v0.5.8 恢复原阈值 4.8%，因仅在持仓状态判断
        df["big_yang"] = (
            df["is_yang"]
            & (df["daily_return"] >= take_profit_pct * 0.6)
            & (df["vol_ratio"] >= 1.5)
        )
        cond_take_profit_1 = df["in_position"] & df["big_yang"]

        # ── 止盈2：高位巨量滞涨（放量 + 长上影 + 涨幅小）→ 清仓 ──
        df["stagnation"] = (
            (df["vol_ratio"] >= stagnation_vol_ratio)
            & (df["upper_shadow"] > max_upper_shadow_pct)
            & (df["daily_return"].abs() < 0.02)
        )
        cond_take_profit_2 = df["in_position"] & df["stagnation"]

        # ── 时间止损：持仓超 time_stop_window 天 + 涨幅 < time_stop_threshold ──
        # 计算自最近买入日以来的收益率（不使用未来数据）
        df["return_since_entry"] = (df["close"] - df["entry_close"]) / df["entry_close"]
        # 持仓天数：从最近买入日开始计算
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

        # ════════════════════════════════════════════════
        # 信号赋值（卖出优先于买入）
        # ════════════════════════════════════════════════
        df["signal"] = 0
        # 买入信号：核心5硬AND + 置信度过滤
        df.loc[buy_cond_final, "signal"] = 1
        # 卖出信号优先（止损1 + 止损2 + 止盈1 + 止盈2 + 时间止损）
        df.loc[
            cond_stop_loss_1 | cond_stop_loss_2
            | cond_take_profit_1 | cond_take_profit_2
            | cond_time_stop,
            "signal",
        ] = -1

        # ════════════════════════════════════════════════
        # 置信度赋值
        # ════════════════════════════════════════════════
        df["confidence"] = 0.0
        # 核心信号用评分（clip 0.5-0.85）
        df.loc[buy_cond_final, "confidence"] = df.loc[buy_cond_final, "confidence_score"].clip(0.5, 0.85)

        return df
