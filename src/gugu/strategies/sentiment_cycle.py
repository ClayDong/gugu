"""情绪周期策略（sentiment_cycle）。

来源：MingCe 明策系统移植 + 钱多多情绪周期方法论。

核心逻辑：用波动率+量能+动量合成情绪指标，在冰点买入、高潮卖出。
- 情绪冰点（恐惧）→ 试探性买入
- 情绪回暖 → 加仓信号
- 情绪高潮（贪婪）→ 减仓离场
- 情绪退潮 → 空仓等待

这与钱多多的"情绪周期"完全一致：中军无力、跟随龙不涨停、
龙头独木难支=情绪退潮=撤退信号。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy
from gugu.utils.log import get_logger

logger = get_logger()

__all__ = ["SentimentCycleStrategy"]


class SentimentCycleStrategy(Strategy):
    """情绪周期策略。

    合成情绪指标 = 波动率倒数 × 量能比 × 动量因子
    - 冰点 < 0.3 → 买入
    - 高潮 > 0.8 → 卖出
    """

    name = "sentiment_cycle"
    category = "trend"

    def validate_params(self) -> None:
        fear = float(self.params.get("fear_threshold", 0.3))
        greed = float(self.params.get("greed_threshold", 0.8))
        if fear >= greed:
            raise ValueError("fear_threshold 必须小于 greed_threshold")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        window = int(self.params.get("sentiment_window", 20))
        momentum_days = int(self.params.get("momentum_days", 10))
        fear_threshold = float(self.params.get("fear_threshold", 0.3))
        greed_threshold = float(self.params.get("greed_threshold", 0.8))
        trailing_stop_pct = float(self.params.get("trailing_stop_pct", 0.08))

        if len(df) < window:
            return df

        # ── 情绪指标合成 ──────────────────────────────
        # 1. 波动率因子：波动率越低，情绪越稳定（冰点=高波动=恐惧）
        volatility = df["close"].pct_change().rolling(window).std()
        avg_volatility = volatility.expanding().mean()
        vol_factor = 1 - (volatility / avg_volatility.clip(lower=1e-8))

        # 2. 量能因子：量能越大，情绪越高涨
        volume_ratio = df["volume"] / df["volume"].rolling(window).mean().clip(lower=1e-8)
        vol_factor_2 = volume_ratio / (1 + volume_ratio)

        # 3. 动量因子：近期涨幅越大，情绪越高涨
        price_momentum = df["close"].pct_change(momentum_days)
        momentum_factor = (1 + price_momentum.clip(-1, 1)) / 2

        # 合成情绪指标（0=极度恐惧，1=极度贪婪）
        df["sentiment"] = (vol_factor * 0.4 + vol_factor_2 * 0.3 + momentum_factor * 0.3).clip(0, 1)

        # ── 信号生成 ──────────────────────────────────
        df["signal"] = 0
        prev_sentiment = df["sentiment"].shift(1)

        # 买入：情绪从冰点以上跌入冰点（恐惧→试探性买入）
        fear_cross = (df["sentiment"] < fear_threshold) & (prev_sentiment >= fear_threshold)
        df.loc[fear_cross, "signal"] = 1

        # 卖出：情绪从高潮以上回落（贪婪→减仓）
        greed_cross = (df["sentiment"] > greed_threshold) & (prev_sentiment <= greed_threshold)
        df.loc[greed_cross, "signal"] = -1

        # 止损卖出：情绪回暖后又快速回落（退潮）
        cool_down = (df["sentiment"] > 0.6) & (prev_sentiment <= 0.6) & (df["signal"] != 1)
        df.loc[cool_down, "signal"] = -1

        # ── 置信度 ────────────────────────────────────
        df["confidence"] = 0.0
        df.loc[fear_cross, "confidence"] = (
            (fear_threshold - df["sentiment"]) / fear_threshold
        ).clip(0, 1)
        df.loc[greed_cross, "confidence"] = (
            (df["sentiment"] - greed_threshold) / (1 - greed_threshold)
        ).clip(0, 1)

        # ── 移动止损 ──────────────────────────────────
        if trailing_stop_pct > 0:
            df = self._apply_trailing_stop_exit(df, trailing_stop_pct)

        buy_count = int(np.sum(df["signal"] == 1))
        sell_count = int(np.sum(df["signal"] == -1))
        logger.info(
            f"sentiment_cycle: {buy_count} 买入, {sell_count} 卖出 | "
            f"sentiment range=[{df['sentiment'].min():.2f}, {df['sentiment'].max():.2f}]"
        )

        return df
