"""云策略：三维趋势判定 + 两类入场买点 + 严格止损 + 分层仓位。

核心哲学：
- 只做上涨趋势，震荡/下跌空仓等待
- 三维趋势共振（均线多头 + 高低点抬升 + 量价配合）缺一不可
- 两类入场：回踩均线低吸 + 放量突破平台
- 严格止损：单笔亏损≤总资金2% + 跌破20日线清仓
- 分层仓位：趋势初期4成、确认6-7成、末期2成

第一性原理：
- 趋势是散户唯一的朋友，逆势操作是亏损根源
- 共振是信号质量的保证，单一维度不足以确认趋势
- 止损是生存的底线，没有止损就没有复利
- 仓位管理是收益曲线的稳定器，禁止梭哈逆势补仓
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class CloudTrendStrategy(Strategy):
    """云策略：三维趋势判定 + 两类入场 + 严格止损 + 分层仓位。

    三维趋势判定：
    1. 均线多头：MA20斜率向上（MA20 > MA20.shift(5)）
    2. 高低点抬升：近20日高点抬高 + 近20日低点抬高
    3. 量价配合：近10日阳线均量 > 近10日阴线均量

    两类入场买点：
    A. 低吸买点：回踩MA10或MA20 + 收阳/十字星 + 三维趋势全满足
    B. 突破买点：放量突破20日高点 + 至少二维趋势满足

    止损规则：
    - 单笔亏损≤总资金2%（通过建议仓位比例体现）
    - 跌破MA20清仓

    分层仓位：
    - 趋势初期（三维刚满足≤5日）：40%
    - 趋势确认（三维持续>5日）：60%
    - 趋势末期（任一维度弱化）：20%
    """

    name = "cloud_trend"
    category = "trend"

    def __init__(self, params=None):
        super().__init__(params)
        # P1修复：移动止盈参数（从持仓最高点回落比例强制卖出）
        # 默认 trailing_stop_pct=0.05 代表从最高点回落5%触发止盈
        self._trailing_stop_pct = float(self.params.get("trailing_stop_pct", 0.05))
        self._time_stop_days = int(self.params.get("time_stop_days", 15))

    def validate_params(self) -> None:
        ma_short = int(self.params.get("ma_short", 10))
        ma_trend = int(self.params.get("ma_trend", 20))
        if ma_short >= ma_trend:
            raise ValueError("ma_short 必须小于 ma_trend")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        ma_short = int(self.params.get("ma_short", 10))
        ma_trend = int(self.params.get("ma_trend", 20))
        hh_ll_window = int(self.params.get("hh_ll_window", 20))
        hh_ll_compare = int(self.params.get("hh_ll_compare", 5))
        vol_compare_window = int(self.params.get("vol_compare_window", 10))
        breakout_vol_ratio = float(self.params.get("breakout_vol_ratio", 1.5))
        pullback_tolerance = float(self.params.get("pullback_tolerance", 0.01))
        doji_body_max = float(self.params.get("doji_body_max", 0.01))
        small_yang_max = float(self.params.get("small_yang_max_gain", 0.03))
        trend_early_days = int(self.params.get("trend_early_days", 5))

        # ── 计算均线 ──────────────────────────────────
        df["ma_short"] = df["close"].rolling(ma_short).mean()
        df["ma_trend"] = df["close"].rolling(ma_trend).mean()
        df["vol_ma"] = df["volume"].rolling(ma_trend).mean()

        # ── 计算高低点 ──────────────────────────────────
        df["hh"] = df["high"].rolling(hh_ll_window).max()
        df["ll"] = df["low"].rolling(hh_ll_window).min()

        # ── 三维趋势判定 ────────────────────────────────

        # 维度1：均线多头 — MA20斜率向上
        df["ma_trend_slope"] = df["ma_trend"] - df["ma_trend"].shift(hh_ll_compare)
        df["dim1_ma_bull"] = df["ma_trend_slope"] > 0

        # 维度2：高低点抬升 — 近20日高点和低点都在抬高
        df["dim2_hh_rising"] = df["hh"] > df["hh"].shift(hh_ll_compare)
        df["dim2_ll_rising"] = df["ll"] > df["ll"].shift(hh_ll_compare)
        df["dim2_hl_rising"] = df["dim2_hh_rising"] & df["dim2_ll_rising"]

        # 维度3：量价配合 — 近10日阳线均量 > 近10日阴线均量
        df["is_yang"] = df["close"] > df["open"]
        df["is_yin"] = df["close"] < df["open"]

        def _rolling_cond_mean(series: pd.Series, condition: pd.Series, window: int) -> pd.Series:
            """计算满足条件的滚动均值。"""
            result = pd.Series(np.nan, index=series.index)
            for i in range(window - 1, len(series)):
                window_slice = series.iloc[i - window + 1 : i + 1]
                cond_slice = condition.iloc[i - window + 1 : i + 1]
                if cond_slice.any():
                    result.iloc[i] = window_slice[cond_slice].mean()
                else:
                    result.iloc[i] = 0.0
            return result

        df["yang_vol_avg"] = _rolling_cond_mean(df["volume"], df["is_yang"], vol_compare_window)
        df["yin_vol_avg"] = _rolling_cond_mean(df["volume"], df["is_yin"], vol_compare_window)
        df["dim3_vol_healthy"] = df["yang_vol_avg"] > df["yin_vol_avg"]

        # 三维趋势综合判定
        df["trend_dims_count"] = (
            df["dim1_ma_bull"].astype(int)
            + df["dim2_hl_rising"].astype(int)
            + df["dim3_vol_healthy"].astype(int)
        )
        df["trend_all_3"] = df["trend_dims_count"] == 3
        df["trend_at_least_2"] = df["trend_dims_count"] >= 2

        # ── 趋势持续性（用于分层仓位） ──────────────────
        df["trend_all_3_days"] = 0
        consecutive = 0
        for i in range(len(df)):
            if df["trend_all_3"].iloc[i]:
                consecutive += 1
            else:
                consecutive = 0
            df.iloc[i, df.columns.get_loc("trend_all_3_days")] = consecutive

        # ── 趋势弱化检测（用于趋势末期判断） ────────────
        df["dim1_weakening"] = df["dim1_ma_bull"] & (
            df["ma_trend_slope"] < df["ma_trend_slope"].shift(3)
        )
        df["dim2_weakening"] = df["dim2_hl_rising"] & (
            (df["hh"] - df["hh"].shift(1)) < (df["hh"].shift(1) - df["hh"].shift(2))
        )
        df["any_dim_weakening"] = df["dim1_weakening"] | df["dim2_weakening"]

        # ── 初始化信号 ──────────────────────────────────
        df["signal"] = 0
        df["confidence"] = 0.0
        df["suggested_position_ratio"] = 0.0

        # ── 入场买点A：低吸买点 ─────────────────────────
        near_ma10 = df["close"] <= df["ma_short"] * (1 + pullback_tolerance)
        near_ma20 = df["close"] <= df["ma_trend"] * (1 + pullback_tolerance)
        above_ma20 = df["close"] >= df["ma_trend"] * (1 - pullback_tolerance)
        pullback_to_ma = (near_ma10 | near_ma20) & above_ma20

        body_pct = abs(df["close"] - df["open"]) / df["open"].replace(0, np.nan)
        is_doji = body_pct <= doji_body_max
        is_small_yang = (df["close"] > df["open"]) & (body_pct <= small_yang_max)
        reversal_candle = is_doji | is_small_yang

        prev_yin = df["is_yin"].shift(1).fillna(False)
        prev2_yin = df["is_yin"].shift(2).fillna(False)
        double_yin_before = prev_yin & prev2_yin
        double_yin_reversal = double_yin_before & reversal_candle

        pullback_buy = pullback_to_ma & reversal_candle & df["trend_all_3"]

        # ── 入场买点B：突破买点 ─────────────────────────
        breakout_high = df["close"] > df["hh"].shift(1)
        volume_surge = df["volume"] > df["vol_ma"] * breakout_vol_ratio
        breakout_buy = breakout_high & volume_surge & df["trend_at_least_2"]

        # ── 合并买入信号 ────────────────────────────────
        buy_mask = pullback_buy | breakout_buy
        df.loc[buy_mask, "signal"] = 1

        # ── 持仓状态机：买入后跟踪，卖出后重置 ─────────
        # 修复：原来"跌破MA20就全量发卖出信号"导致596:12的卖出/买入比
        # 修复思路：只有"持有状态"下跌破MA20才发卖出信号
        # P1修复：增加移动止盈（从最高点回落trailing_stop_pct强制卖出）
        # P1修复：增加时间止盈（持仓超time_stop_d天且收益为正时卖出）
        df["in_position"] = False
        position_held = False
        entry_price = 0.0
        highest_price = 0.0
        hold_days_count = 0
        for i in range(len(df)):
            if df["signal"].iloc[i] == 1:
                position_held = True
                entry_price = float(df["close"].iloc[i])
                highest_price = entry_price
                hold_days_count = 0
            elif df["signal"].iloc[i] == -1:
                position_held = False
                entry_price = 0.0
                highest_price = 0.0
                hold_days_count = 0

            if position_held:
                hold_days_count += 1
                current_close = float(df["close"].iloc[i])
                if current_close > highest_price:
                    highest_price = current_close
                df.iloc[i, df.columns.get_loc("in_position")] = True

        # ── 卖出信号（仅持仓状态下检查） ────────────────
        df["below_ma20"] = df["close"] < df["ma_trend"]

        # 移动止盈：从最高点回落超过trailing_stop_pct
        df["trailing_stop_active"] = False
        df["time_stop_active"] = False

        position_held = False
        entry_price = 0.0
        highest_price = 0.0
        hold_days_count = 0
        for i in range(len(df)):
            if df["signal"].iloc[i] == 1:
                position_held = True
                entry_price = float(df["close"].iloc[i])
                highest_price = entry_price
                hold_days_count = 0
            elif df["signal"].iloc[i] == -1:
                position_held = False
                entry_price = 0.0
                highest_price = 0.0
                hold_days_count = 0

            if position_held:
                hold_days_count += 1
                current_close = float(df["close"].iloc[i])
                if current_close > highest_price:
                    highest_price = current_close
                pullback_ratio = (
                    (highest_price - current_close) / highest_price if highest_price > 0 else 0
                )
                df.iloc[i, df.columns.get_loc("trailing_stop_active")] = (
                    pullback_ratio >= self._trailing_stop_pct
                )
                df.iloc[i, df.columns.get_loc("time_stop_active")] = (
                    hold_days_count >= self._time_stop_days and current_close >= entry_price * 0.98
                )

        trailing_stop = df["in_position"] & df["trailing_stop_active"]
        time_stop = df["in_position"] & df["time_stop_active"]
        exit_mask = (df["in_position"] & df["below_ma20"]) | trailing_stop | time_stop
        # 防止exit_mask覆盖买入信号（买入当天不应立即卖出）
        exit_mask = exit_mask & (df["signal"] != 1)
        df.loc[exit_mask, "signal"] = -1

        # ── 置信度计算 ──────────────────────────────────
        # 低吸买点置信度
        pullback_conf = np.where(
            pullback_buy,
            (
                0.35 * (double_yin_reversal.astype(float))  # 双阴反转加强 35%
                + 0.25
                * (1 - abs(df["close"] - df["ma_trend"]) / df["ma_trend"].replace(0, np.nan)).clip(
                    0, 1
                )  # 回踩精准度 25%
                + 0.25 * (df["trend_all_3_days"] / trend_early_days).clip(0, 1)  # 趋势持续性 25%
                + 0.15 * (df["dim3_vol_healthy"].astype(float))  # 量价健康度 15%
            ).clip(0, 1),
            0.0,
        )

        # 突破买点置信度
        breakout_conf = np.where(
            breakout_buy,
            (
                0.35
                * (
                    (df["volume"] / df["vol_ma"].replace(0, np.nan)) / (breakout_vol_ratio * 2)
                ).clip(0, 1)  # 放量强度 35%
                + 0.30 * (df["trend_dims_count"] / 3)  # 趋势维度数 30%
                + 0.20
                * (
                    (df["close"] - df["hh"].shift(1)) / df["hh"].shift(1).replace(0, np.nan) / 0.03
                ).clip(0, 1)  # 突破幅度 20%
                + 0.15 * (df["trend_all_3_days"] / trend_early_days).clip(0, 1)  # 趋势持续性 15%
            ).clip(0, 1),
            0.0,
        )

        df["confidence"] = np.where(pullback_buy, pullback_conf, breakout_conf)
        df.loc[df["signal"] == 0, "confidence"] = 0.0

        # 卖出置信度
        sell_mask = df["signal"] == -1
        df.loc[sell_mask, "confidence"] = 0.75

        # ── 分层仓位 ────────────────────────────────────
        # 趋势初期（三维刚满足≤5日）：40%
        # 趋势确认（三维持续>5日）：60%
        # 趋势末期（任一维度弱化）：20%
        early_stage = (df["trend_all_3_days"] > 0) & (df["trend_all_3_days"] <= trend_early_days)
        confirmed_stage = df["trend_all_3_days"] > trend_early_days
        late_stage = df["any_dim_weakening"] & df["trend_at_least_2"]

        df.loc[buy_mask & early_stage, "suggested_position_ratio"] = 0.40
        df.loc[buy_mask & confirmed_stage & ~late_stage, "suggested_position_ratio"] = 0.60
        df.loc[buy_mask & late_stage, "suggested_position_ratio"] = 0.20

        # 突破买点在趋势确认阶段可以给更高仓位
        df.loc[breakout_buy & confirmed_stage & ~late_stage, "suggested_position_ratio"] = 0.70

        return df
