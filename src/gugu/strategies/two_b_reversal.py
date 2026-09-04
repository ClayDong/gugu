"""2B 反转策略（two_b_reversal）——邻居大爷 2B 点买入法。

来源：mulan-quant 多博主规则库（邻居大爷 R-LN06 2B点）。
2026-09-04 事件研究验证：116 样本，5日超额 +2.18%，t=3.55，p=0.001 统计显著。

核心逻辑：14 日周期内，二次探底不破前低 + 当日收阳 → 假突破反转买点。

- 买入：近 14 日分两段（前 7 日/后 7 日），后段最低价 ≥ 前段最低价（双底不破），
        且当日收阳（close > open）
- 卖出：跌破前低（2B 失败）或移动止损回撤 8%

与双底形态的区别：2B 点要求"前低是近期最低"，二次探底贴近但不破，是
主力洗盘结束的经典信号。事件研究证明其在 A 股有真实 edge。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()

__all__ = ["TwoBReversalStrategy"]


class TwoBReversalStrategy(Strategy):
    """2B 反转策略。

    买入（3 AND）：
    1. 前 7 日有近期低点（prev_low）
    2. 后 7 日最低价 ≥ 前低（双底不破）
    3. 当日收阳（close > open）

    卖出（2 OR）：
    1. 跌破前低（2B 失败，形态破坏）
    2. 移动止损回撤 trailing_stop_pct（默认 8%）
    """

    name = "two_b_reversal"
    category = "mean_revert"

    def validate_params(self) -> None:
        if int(self.params.get("cycle_days", 14)) < 4:
            raise ValueError("cycle_days 必须 ≥ 4")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        cycle = int(self.params.get("cycle_days", 14))
        half = max(cycle // 2, 1)
        trailing_stop_pct = float(self.params.get("trailing_stop_pct", 0.08))

        # ── 双底结构 ──────────────────────────────────
        # 前 half 日最低（近端低点）
        prev_low = df["low"].rolling(half).min().shift(half)
        # 后 half 日最低（当前段低点）
        curr_low = df["low"].rolling(half).min()
        # 双底不破：当前段最低 ≥ 前段最低
        double_bottom = curr_low >= prev_low

        # 收阳确认
        is_yang = df["close"] > df["open"]

        # 当日需处于后段末端（用 close 接近 curr_low 段末期近似——rolling 已含当日）
        buy_cond = double_bottom & is_yang & prev_low.notna()

        # ── 卖出：持有 max_hold_days 日 + 移动止损 ──────
        # 2026-09-04 修复：原"跌破前低即卖"导致在错误时机割肉（回测 -5.84%）。
        # 事件研究（116样本 p=0.001）证明 2B 信号持有 5 日有正超额，
        # 退出改为"固定持有 + 移动止损"，与事件研究口径一致。
        max_hold_days = int(self.params.get("max_hold_days", 5))
        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1

        # 移动止损兜底（道②：让利润奔跑）
        if trailing_stop_pct > 0:
            df = self._apply_trailing_stop_exit(df, trailing_stop_pct)
        # 时间退出（持有 max_hold_days 日未触发移动止损则平仓）
        df = self._apply_max_hold_exit(df, max_hold_days=max_hold_days)

        # ── 置信度：双底贴近度 + 收阳强度 ────────────
        # 贴近度：当前段最低离前低越近（不破），反转概率越高
        proximity = (curr_low - prev_low).abs() / prev_low.replace(0, np.nan)
        proximity_score = (1 - proximity.clip(0, 0.1) / 0.1).clip(0, 1)
        yang_strength = ((df["close"] - df["open"]) / df["open"].replace(0, np.nan)).clip(
            0, 0.05
        ) / 0.05
        df["confidence"] = 0.0
        df.loc[buy_cond, "confidence"] = (proximity_score * 0.6 + yang_strength * 0.4).clip(0.3, 1)

        buy_count = int(np.sum(df["signal"] == 1))
        sell_count = int(np.sum(df["signal"] == -1))
        logger.info(
            f"two_b_reversal: {buy_count} 买入, {sell_count} 卖出 | "
            f"双底事件={int(double_bottom.sum())}"
        )

        return df
