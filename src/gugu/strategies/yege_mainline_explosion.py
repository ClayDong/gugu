"""野哥主线爆发战法（研究版）。

该策略把主线事件证据作为入场前提，个股 OHLCV 只做候选股技术确认。
没有结构化事件证据时不产生买入信号，避免把单股独立上涨冒充主线爆发。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()

__all__ = ["YegeMainlineExplosionStrategy"]


class YegeMainlineExplosionStrategy(Strategy):
    """主线爆发后聚焦核心/空间龙候选。"""

    name = "yege_mainline_explosion"
    category = "main_force"

    def validate_params(self) -> None:
        if int(self.params.get("ma_short", 5)) <= 0:
            raise ValueError("ma_short 必须 > 0")
        if int(self.params.get("ma_mid", 20)) <= int(self.params.get("ma_short", 5)):
            raise ValueError("ma_mid 必须大于 ma_short")
        if (
            not 3
            <= int(self.params.get("min_first_board_count", 3))
            <= int(self.params.get("max_first_board_count", 5))
        ):
            raise ValueError("首板数量范围必须在 3-5 且下限不大于上限")
        if (
            not 3
            <= int(self.params.get("first_board_window_min", 3))
            <= int(self.params.get("first_board_window_max", 60))
        ):
            raise ValueError("早盘窗口必须覆盖 3-60 分钟")
        if float(self.params.get("trailing_stop_pct", 0.08)) <= 0:
            raise ValueError("trailing_stop_pct 必须 > 0")

    @staticmethod
    def _context_series(df: pd.DataFrame, name: str, default: object) -> pd.Series:
        if name in df.columns:
            return df[name]
        return pd.Series([default] * len(df), index=df.index)

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()
        ma_short = int(self.params.get("ma_short", 5))
        ma_mid = int(self.params.get("ma_mid", 20))
        momentum_days = int(self.params.get("momentum_days", 3))
        volume_ratio_min = float(self.params.get("volume_ratio_min", 1.1))
        pullback_tolerance = float(self.params.get("pullback_tolerance", 0.04))
        min_message_level = str(self.params.get("min_message_level", "high")).lower()
        min_anchor_market_cap_yi = float(self.params.get("min_anchor_market_cap_yi", 100.0))
        min_sector_volume_ratio = float(self.params.get("min_sector_volume_ratio", 1.2))
        breakout_window = int(self.params.get("breakout_window", 20))
        trailing_stop_pct = float(self.params.get("trailing_stop_pct", 0.08))
        sector_high = self._context_series(df, "yege_sector_index_high", np.nan)
        sector_close = self._context_series(df, "yege_sector_index_close", np.nan)
        sector_high_check = pd.to_numeric(sector_high, errors="coerce")
        sector_close_check = pd.to_numeric(sector_close, errors="coerce")
        sector_window_high = (
            sector_high_check.rolling(breakout_window, min_periods=breakout_window).max().shift(1)
        )

        df["yege_ma_short"] = df["close"].rolling(ma_short).mean()
        df["yege_ma_mid"] = df["close"].rolling(ma_mid).mean()
        df["yege_volume_ratio"] = df["volume"] / df["volume"].rolling(ma_mid).mean().shift(1)
        df["yege_momentum"] = df["close"].pct_change(momentum_days)

        # 事件证据必须由研究/引擎层显式注入；缺失值一律不买。
        event_passed = (
            self._context_series(df, "yege_event_passed", False).fillna(False).astype(bool)
        )
        entry_ready = self._context_series(df, "yege_entry_ready", False).fillna(False).astype(bool)
        evidence = (
            self._context_series(df, "yege_evidence_status", "unavailable")
            .fillna("unavailable")
            .astype(str)
        )
        candidate = self._context_series(df, "yege_candidate", False).fillna(False).astype(bool)
        board_role = self._context_series(df, "yege_board_role", "").fillna("").astype(str)
        valid_role = board_role.isin(["space_dragon", "core", "leader_candidate"])
        evidence_available = evidence.eq("available")
        message_level = self._context_series(df, "yege_message_level", "").fillna("").astype(str)
        message_rank = {"low": 1, "medium": 2, "high": 3}
        message_confirmed = message_level.map(message_rank).fillna(0) >= message_rank.get(
            min_message_level, 3
        )
        anchor_cap = pd.to_numeric(
            self._context_series(df, "yege_max_anchor_market_cap_yi", np.nan), errors="coerce"
        )
        sector_volume = pd.to_numeric(
            self._context_series(df, "yege_sector_volume_ratio", np.nan), errors="coerce"
        )
        sector_breakout = (
            self._context_series(df, "yege_sector_index_new_high", False).fillna(False).astype(bool)
        )
        breakout_confirmed = sector_breakout & (sector_volume >= min_sector_volume_ratio)
        if sector_close_check.notna().any() and sector_window_high.notna().any():
            breakout_confirmed &= sector_close_check >= sector_window_high
        anchor_confirmed = anchor_cap >= min_anchor_market_cap_yi
        board_window = pd.to_numeric(
            self._context_series(df, "yege_first_board_window_minutes", np.nan), errors="coerce"
        )
        window_confirmed = board_window.between(
            int(self.params.get("first_board_window_min", 3)),
            int(self.params.get("first_board_window_max", 60)),
            inclusive="both",
        )
        first_board_count = pd.to_numeric(
            self._context_series(df, "yege_first_board_count", np.nan), errors="coerce"
        )
        board_count_confirmed = first_board_count.between(
            int(self.params.get("min_first_board_count", 3)),
            int(self.params.get("max_first_board_count", 5)),
            inclusive="both",
        )
        old_dragon_confirmed = (
            self._context_series(df, "yege_old_dragon_anomaly", False).fillna(False).astype(bool)
        )

        trend = (df["close"] > df["yege_ma_short"]) & (
            df["yege_ma_short"] > df["yege_ma_short"].shift(1)
        )
        above_mid = df["close"] > df["yege_ma_mid"]
        momentum = df["yege_momentum"] > 0
        volume_confirmed = df["yege_volume_ratio"] >= volume_ratio_min
        near_ma = (df["close"] - df["yege_ma_short"]).abs() / df["yege_ma_short"].replace(
            0, np.nan
        ) <= pullback_tolerance

        buy_cond = (
            event_passed
            & entry_ready
            & evidence_available
            & message_confirmed
            & breakout_confirmed
            & anchor_confirmed
            & window_confirmed
            & board_count_confirmed
            & old_dragon_confirmed
            & candidate
            & valid_role
            & trend
            & above_mid
            & momentum
            & volume_confirmed
            & near_ma
        ).fillna(False)

        sell_raw = (df["close"] < df["yege_ma_short"]) | (df["yege_momentum"] < -pullback_tolerance)
        buy_arr = buy_cond.to_numpy(dtype=bool)
        sell_arr = sell_raw.fillna(False).to_numpy(dtype=bool)
        in_position = False
        valid_sell = np.zeros(len(df), dtype=bool)
        for i in range(len(df)):
            if buy_arr[i]:
                in_position = True
            elif sell_arr[i] and in_position:
                valid_sell[i] = True
                in_position = False

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[pd.Series(valid_sell, index=df.index), "signal"] = -1

        confidence = (
            (df["yege_momentum"].clip(lower=0) / 0.05).clip(0, 1) * 0.35
            + ((df["yege_volume_ratio"] - 1) / 1.5).clip(0, 1) * 0.25
            + trend.astype(float) * 0.20
            + (message_confirmed & breakout_confirmed & anchor_confirmed).astype(float) * 0.20
        ).clip(0, 1)
        df["confidence"] = confidence.where(df["signal"] != 0, 0.0)

        if trailing_stop_pct > 0:
            df = self._apply_trailing_stop_exit(df, trailing_stop_pct)

        logger.info(
            "yege_mainline_explosion 信号统计: {} 买入, {} 卖出, evidence_available={}",
            int((df["signal"] == 1).sum()),
            int((df["signal"] == -1).sum()),
            int(evidence_available.sum()),
        )
        return df
