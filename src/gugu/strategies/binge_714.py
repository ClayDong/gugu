"""斌哥714战法。

来源：斌哥Bg视频（2026-08-29）转写文档。

核心三步：
- 7：7连阳打底（主力持续吸筹），排除三连板以上
- 1：异动倍量阳（冲击涨停后炸板，长上影阳线，量≥前日2倍）
- 4：随后4天缩量确认（不破7连阳首根最低价，量持续萎缩，换手3%-9%）

买入条件：7+1+4全部满足后买入
卖出条件：跌破7连阳首根最低价 或 移动止损8%
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()

__all__ = ["Binge714Strategy"]


class Binge714Strategy(Strategy):
    """斌哥714战法。"""

    name = "binge_714"
    category = "main_force"

    def validate_params(self) -> None:
        if int(self.params.get("consecutive_yang_min", 7)) < 3:
            raise ValueError("consecutive_yang_min 必须 >= 3")
        if float(self.params.get("vol_ratio_min", 2.0)) < 1.0:
            raise ValueError("vol_ratio_min 必须 >= 1.0")
        if int(self.params.get("confirm_days", 4)) < 2:
            raise ValueError("confirm_days 必须 >= 2")
        if float(self.params.get("trailing_stop_pct", 0.08)) <= 0:
            raise ValueError("trailing_stop_pct 必须 > 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        consecutive_min = int(self.params.get("consecutive_yang_min", 7))
        vol_ratio_min = float(self.params.get("vol_ratio_min", 2.0))
        confirm_days = int(self.params.get("confirm_days", 4))
        turnover_min = float(self.params.get("turnover_min", 0.03))
        turnover_max = float(self.params.get("turnover_max", 0.09))
        trailing_stop_pct = float(self.params.get("trailing_stop_pct", 0.08))
        max_consecutive_boards = int(self.params.get("max_consecutive_boards", 3))

        df["daily_ret"] = df["close"].pct_change()
        df["is_yang"] = df["close"] > df["open"]
        df["vol_ma5"] = df["volume"].rolling(5).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # 涨停近似（涨幅 > 9.5%）
        is_limit_up = df["daily_ret"] > 0.095
        consecutive_boards = is_limit_up.rolling(max_consecutive_boards).sum()
        too_many_boards = consecutive_boards >= max_consecutive_boards

        # 7连阳检测
        yang_streak = df["is_yang"].rolling(consecutive_min).sum()
        has_7_yang = yang_streak >= consecutive_min

        # 异动倍量阳：长上影阳线 + 量≥前日2倍
        upper_shadow = (df["high"] - df[["open", "close"]].max(axis=1)) / df["close"].replace(
            0, np.nan
        )
        is_anomaly_yang = (
            df["is_yang"]
            & (upper_shadow > 0.02)
            & (df["vol_ratio"] >= vol_ratio_min)
            & df["daily_ret"].between(0.03, 0.095)
        )

        # 4天缩量确认
        n = len(df)
        signal = np.zeros(n, dtype=int)
        entry_low = 0.0

        for i in range(consecutive_min + 1, n):
            # 检查7连阳是否在 i-1 结束
            yang_end = i - 1
            if not has_7_yang.iloc[yang_end]:
                continue
            if too_many_boards.iloc[yang_end]:
                continue

            # 7连阳首根最低价
            start_idx = yang_end - consecutive_min + 1
            entry_low = float(df["low"].iloc[start_idx])

            # 异动倍量阳在7连阳期间出现
            anomaly_in_range = is_anomaly_yang.iloc[start_idx : yang_end + 1]
            if not anomaly_in_range.any():
                continue

            # 4天缩量确认
            confirm_end = min(yang_end + confirm_days + 1, n)
            if confirm_end >= n:
                continue
            confirm_slice = df.iloc[yang_end + 1 : confirm_end]
            if len(confirm_slice) < confirm_days:
                continue

            # 条件1：不破7连阳首根最低价
            if confirm_slice["close"].min() < entry_low:
                continue

            # 条件2：缩量（不超过异动日量）
            anomaly_vol = float(df["volume"].iloc[yang_end])
            if confirm_slice["volume"].max() > anomaly_vol:
                continue

            # 条件3：换手率3%-9%（用 vol_ratio 近似映射到 turnover 范围）
            confirm_vol_ratio = confirm_slice["vol_ratio"].dropna()
            if len(confirm_vol_ratio) == 0:
                continue
            avg_ratio = float(confirm_vol_ratio.mean())
            # vol_ratio 近似换手率：ratio 0.8~3.0 对应 turnover 3%~9%
            ratio_min = turnover_min / 0.03 * 0.8
            ratio_max = turnover_max / 0.09 * 3.0
            if avg_ratio < ratio_min or avg_ratio > ratio_max:
                continue

            # 买入信号在确认结束日
            signal[confirm_end] = 1

            # 卖出：跌破7连阳首根最低价
            for j in range(confirm_end + 1, n):
                if df["close"].iloc[j] < entry_low:
                    signal[j] = -1
                    break

        df["signal"] = signal
        df["confidence"] = 0.0
        buy_mask = df["signal"] == 1
        df.loc[buy_mask, "confidence"] = 0.7

        if trailing_stop_pct > 0:
            df = self._apply_trailing_stop_exit(df, trailing_stop_pct)

        buy_count = int((df["signal"] == 1).sum())
        sell_count = int((df["signal"] == -1).sum())
        logger.info(
            "binge_714 信号统计: {} 买入, {} 卖出",
            buy_count,
            sell_count,
        )
        return df
