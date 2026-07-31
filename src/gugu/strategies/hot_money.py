"""游资策略：三数交易法 — 涨停筛选 + 缩倍量入场 + 放倍量离场。

核心哲学（源自游资内部交易心法）：
- 涨停是主力进场的信号，7日内涨停过的股票才有资金关注
- 缩量至一半以下 = 不坚定筹码已被清洗，主力仍在场内 → 入场
- 放量至两倍以上 = 主力可能在出货，风险增加 → 离场
- 半仓滚动：阴线买、阳线卖，在上涨趋势中反复操作增厚收益

第一性原理：
- 成交量是唯一无法造假的大资金痕迹（价格可以操纵，量不能）
- 缩量代表筹码锁定（供给枯竭），放量代表筹码松动（供给释放）
- 涨停是主力态度的最强信号（愿意用涨停价吃货 = 强烈看多）
- 半仓滚动 = 底仓吃趋势 + 滚动仓吃波动，鱼和熊掌兼得

三步法：
1. 筛选：近7日内出现过涨停 → 加入观察池
2. 入场：成交量缩量至涨停日量能的50%以下 → 买入信号
3. 离场：成交量放大至涨停日量能的200%以上 → 卖出信号

进阶：半仓滚动
- 持仓期间，阴线日加仓（滚动买入），阳线日减仓（滚动卖出）
- 始终保持底仓不变，只用滚动仓做T

v0.6.2 优化：
- P0修复：增加5%硬止损 + 7%移动止盈（从持仓最高点回落）
- P0修复：参考量能向后滚动传播（突破7天窗口不消失，使用max_track_days控制）
- P1修复：双信号通道（涨停≥9.5% + 大阳线≥5%均可作为主力信号）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class HotMoneyStrategy(Strategy):
    """游资策略：三数交易法。

    三步法：
    1. 筛选：近N日内出现过涨停（涨幅≥9.5%或达到涨停价）
       或大阳线（涨幅≥big_yang_threshold，默认5%）
    2. 入场：成交量缩量至参考量能的shrink_ratio以下
    3. 离场：成交量放大至参考量能的expand_ratio以上

    进阶：半仓滚动 + 止损止盈
    - 持仓期间阴线买入（加仓信号），阳线卖出（减仓信号）
    - 5%硬止损 + 从最高点回落7%移动止盈
    - 参考量能在max_track_days内持续传播，直到新主力信号替代
    """

    name = "hot_money"
    category = "main_force"

    def validate_params(self) -> None:
        limit_up_window = int(self.params.get("limit_up_window", 7))
        if limit_up_window < 3:
            raise ValueError("limit_up_window 至少为 3")
        shrink_ratio = float(self.params.get("shrink_ratio", 0.5))
        if not 0 < shrink_ratio < 1:
            raise ValueError("shrink_ratio 必须在 (0, 1) 之间")
        expand_ratio = float(self.params.get("expand_ratio", 2.0))
        if expand_ratio <= 1:
            raise ValueError("expand_ratio 必须 > 1")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        limit_up_window = int(self.params.get("limit_up_window", 7))
        limit_up_threshold = float(self.params.get("limit_up_threshold", 0.095))
        big_yang_threshold = float(self.params.get("big_yang_threshold", 0.05))
        shrink_ratio = float(self.params.get("shrink_ratio", 0.5))
        expand_ratio = float(self.params.get("expand_ratio", 2.0))
        vol_ma_window = int(self.params.get("vol_ma_window", 20))
        min_volume = float(self.params.get("min_volume", 1_000_000))
        rolling_enabled = bool(self.params.get("rolling_enabled", True))
        rolling_yin_buy_ratio = float(self.params.get("rolling_yin_buy_ratio", 0.5))
        rolling_yang_sell_ratio = float(self.params.get("rolling_yang_sell_ratio", 0.5))
        max_hold_days = int(self.params.get("max_hold_days", 20))
        max_track_days = int(self.params.get("max_track_days", 30))
        big_yang_enabled = bool(self.params.get("big_yang_enabled", True))

        hard_stop_loss = float(self.params.get("hard_stop_loss_pct", 0.05))
        trailing_stop = float(self.params.get("trailing_stop_pct", 0.07))

        # ── 计算基础指标 ──────────────────────────────
        df["daily_return"] = df["close"].pct_change()
        df["vol_ma"] = df["volume"].rolling(vol_ma_window).mean()

        # ── 步骤1：主力信号检测 ────────────────────────
        df["is_limit_up"] = df["daily_return"] >= limit_up_threshold
        df["is_big_yang"] = df["daily_return"] >= big_yang_threshold
        df["is_main_force_signal"] = df["is_limit_up"]
        if big_yang_enabled:
            df["is_main_force_signal"] = df["is_main_force_signal"] | df["is_big_yang"]

        # ── P0修复：参考量能向后滚动传播 ──────────────
        # ref_volume从最近一次主力信号日开始持续向前传播
        # 直到超过max_track_days或遇到新的主力信号日
        df["ref_volume"] = np.nan
        df["days_since_signal"] = np.nan
        last_signal_vol = np.nan
        last_signal_day = -max_track_days
        for i in range(len(df)):
            if df["is_main_force_signal"].iloc[i]:
                last_signal_vol = float(df["volume"].iloc[i])
                last_signal_day = i
            days_diff = i - last_signal_day
            if last_signal_vol is not None and days_diff <= max_track_days:
                df.iloc[i, df.columns.get_loc("ref_volume")] = last_signal_vol
                df.iloc[i, df.columns.get_loc("days_since_signal")] = days_diff

        # N日内出现过主力信号
        df["has_limit_up"] = df["is_limit_up"].rolling(limit_up_window, min_periods=1).max() > 0
        df["has_main_force"] = (
            df["is_main_force_signal"].rolling(max_track_days, min_periods=1).max() > 0
        )

        # ── 步骤2：缩量入场信号 ────────────────────────
        has_ref_vol = df["ref_volume"].notna() & (df["ref_volume"] > 0)
        volume_shrunk = df["volume"] <= df["ref_volume"] * shrink_ratio
        volume_valid = df["volume"] >= min_volume

        in_tracking = df["has_main_force"] & has_ref_vol
        entry_signal = in_tracking & volume_shrunk & volume_valid

        # ── 步骤3：放量离场信号 ────────────────────────
        volume_expanded = df["volume"] >= df["ref_volume"] * expand_ratio
        exit_signal = in_tracking & volume_expanded & volume_valid

        # ── 初始化信号 ──────────────────────────────────
        df["signal"] = 0
        df["confidence"] = 0.0
        df["suggested_position_ratio"] = 0.0
        df["in_position"] = False

        # ── P0修复：持仓状态机 = 买入后跟踪 + 止损止盈 ──
        position_held = False
        entry_price = 0.0
        highest_price = 0.0
        hold_count = 0
        for i in range(len(df)):
            if exit_signal.iloc[i] and not entry_signal.iloc[i]:
                # 放量离场信号直接卖出（不论是否持仓）
                # 但买入信号优先于放量信号保护（当天买入不应当天卖出）
                df.iloc[i, df.columns.get_loc("signal")] = -1
                position_held = False
                entry_price = 0.0
                highest_price = 0.0
                hold_count = 0

            if entry_signal.iloc[i] and not position_held:
                position_held = True
                entry_price = float(df["close"].iloc[i])
                highest_price = entry_price
                hold_count = 1
                df.iloc[i, df.columns.get_loc("signal")] = 1

            if position_held:
                df.iloc[i, df.columns.get_loc("in_position")] = True
                hold_count += 1
                current_close = float(df["close"].iloc[i])
                if current_close > highest_price:
                    highest_price = current_close

                # 硬止损：亏损超过hard_stop_loss
                loss_ratio = (current_close - entry_price) / entry_price if entry_price > 0 else 0
                if loss_ratio <= -hard_stop_loss:
                    df.iloc[i, df.columns.get_loc("signal")] = -1
                    position_held = False
                    entry_price = 0.0
                    highest_price = 0.0
                    hold_count = 0
                    continue

                # 移动止盈：从最高点回落超过trailing_stop
                pullback = (
                    (highest_price - current_close) / highest_price if highest_price > 0 else 0
                )
                if pullback >= trailing_stop and highest_price > entry_price * 1.02:
                    df.iloc[i, df.columns.get_loc("signal")] = -1
                    position_held = False
                    entry_price = 0.0
                    highest_price = 0.0
                    hold_count = 0
                    continue

                # 时间止盈：持仓超过max_hold_days且不亏
                if hold_count >= max_hold_days and current_close >= entry_price * 0.98:
                    df.iloc[i, df.columns.get_loc("signal")] = -1
                    position_held = False
                    entry_price = 0.0
                    highest_price = 0.0
                    hold_count = 0
                    continue

        # ── 置信度计算 ──────────────────────────────────
        shrink_depth = np.where(
            has_ref_vol & (df["ref_volume"] > 0),
            1 - (df["volume"] / df["ref_volume"]),
            0,
        )
        shrink_depth_norm = (shrink_depth / (1 - shrink_ratio)).clip(0, 1)

        days_score = np.where(
            df["days_since_signal"].notna(),
            1 - (df["days_since_signal"].fillna(max_track_days) / max_track_days),
            0,
        )

        vol_health = np.where(
            df["vol_ma"] > 0,
            (df["volume"] / df["vol_ma"]).clip(0, 2) / 2,
            0,
        )

        buy_conf = (0.40 * shrink_depth_norm + 0.35 * days_score + 0.25 * vol_health).clip(0, 1)

        buy_mask = df["signal"] == 1
        df.loc[buy_mask, "confidence"] = buy_conf[buy_mask]

        # 卖出置信度：放量越明显 → 置信度越高
        expand_depth = np.where(
            has_ref_vol & (df["ref_volume"] > 0),
            (df["volume"] / df["ref_volume"] - 1) / (expand_ratio - 1),
            0,
        )
        sell_conf = (0.6 * expand_depth.clip(0, 1) + 0.4 * days_score).clip(0, 1)

        sell_mask = df["signal"] == -1
        df.loc[sell_mask, "confidence"] = sell_conf[sell_mask]

        # ── 建议仓位 ────────────────────────────────────
        base_ratio = 0.5
        df.loc[buy_mask, "suggested_position_ratio"] = (
            base_ratio + 0.2 * shrink_depth_norm[buy_mask]
        ).clip(0.3, 0.7)

        # ── 进阶：半仓滚动信号 ──────────────────────────
        if rolling_enabled:
            df["is_yang"] = df["close"] > df["open"]
            df["is_yin"] = df["close"] < df["open"]

            no_main_signal = df["signal"] == 0
            rolling_buy = df["is_yin"] & in_tracking & ~exit_signal & no_main_signal & volume_valid
            rolling_sell = (
                df["is_yang"] & in_tracking & ~entry_signal & no_main_signal & volume_valid
            )

            df.loc[rolling_buy, "signal"] = 1
            df.loc[rolling_sell, "signal"] = -1

            df.loc[rolling_buy, "confidence"] = 0.45
            df.loc[rolling_sell, "confidence"] = 0.45

            df.loc[rolling_buy, "suggested_position_ratio"] = rolling_yin_buy_ratio
            df.loc[rolling_sell, "suggested_position_ratio"] = rolling_yang_sell_ratio

        # ── 附加列供飞书卡片使用 ────────────────────────
        df["hm_has_limit_up"] = df["has_limit_up"]
        df["hm_ref_volume"] = df["ref_volume"]
        df["hm_days_since_limit_up"] = df["days_since_signal"]
        df["hm_is_shrink_entry"] = entry_signal
        df["hm_is_expand_exit"] = exit_signal
        df["hm_is_rolling"] = False
        if rolling_enabled:
            df.loc[rolling_buy | rolling_sell, "hm_is_rolling"] = True

        return df
