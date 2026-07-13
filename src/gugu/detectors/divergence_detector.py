"""量价背离检测器：股价创新高但成交量持续萎缩时发出卖出预警。

根据《炒股的智慧》策略体系：
- 量价背离：股价创新高，成交量持续萎缩 → 卖出信号
- 天量滞涨：高位放巨量但不涨 → 危险信号
"""

from __future__ import annotations

from typing import Any

import pandas as pd


class DivergenceDetector:
    """检测量价背离和天量滞涨信号。

    用法::

        detector = DivergenceDetector()
        result = detector.detect(df)
        if result["divergence"]:
            # 发出卖出预警
    """

    def detect_volume_price_divergence(self, df: pd.DataFrame, window: int = 20) -> pd.Series:
        """检测量价背离：股价创窗口新高但成交量萎缩。

        条件：
        1. 近 window 日最高价 > 前 window 日最高价（价格创新高）
        2. 近 5 日均量 < 前 20 日均量的 80%（成交量萎缩）

        Args:
            df: 含 close, volume 列的 DataFrame
            window: 检测窗口（默认 20 个交易日）

        Returns:
            pd.Series，1=量价背离卖出信号，0=无信号
        """
        recent_high = df["close"].rolling(window).max()
        prior_high = df["close"].rolling(window).max().shift(window)

        # 价格创新高
        price_new_high = recent_high > prior_high

        # 成交量萎缩：近5日均量 < 前20日均量的80%
        vol_recent = df["volume"].rolling(5).mean()
        vol_prior = df["volume"].rolling(20).mean().shift(5)
        volume_shrink = vol_recent < vol_prior * 0.8

        divergence = (price_new_high & volume_shrink).astype(int)
        return divergence

    def detect_climax_volume(self, df: pd.DataFrame, window: int = 20) -> pd.Series:
        """检测天量滞涨：成交量 > 20日均量*2 但涨幅 < 1%。

        Args:
            df: 含 close, volume 列的 DataFrame
            window: 均量窗口（默认 20）

        Returns:
            pd.Series，1=天量滞涨危险信号，0=无信号
        """
        vol_ma = df["volume"].rolling(window).mean()
        climax_volume = df["volume"] > vol_ma * 2.0

        # 涨幅计算
        price_change = df["close"].pct_change()
        stagnant = price_change.abs() < 0.01

        climax = (climax_volume & stagnant).astype(int)
        return climax

    def detect(self, df: pd.DataFrame, window: int = 20) -> dict[str, Any]:
        """综合检测量价背离和天量滞涨。

        Args:
            df: 含 close, high, low, volume 列的 DataFrame
            window: 检测窗口

        Returns:
            {"divergence": bool, "climax_volume": bool, "signals": list[str]}
        """
        signals: list[str] = []
        has_divergence = False
        has_climax = False

        if len(df) < window * 2:
            return {
                "divergence": False,
                "climax_volume": False,
                "signals": [],
            }

        div = self.detect_volume_price_divergence(df, window)
        if div.iloc[-1] == 1:
            has_divergence = True
            signals.append("量价背离：股价创新高但成交量持续萎缩")

        climax = self.detect_climax_volume(df, window)
        if climax.iloc[-1] == 1:
            has_climax = True
            signals.append("天量滞涨：高位放巨量但不涨")

        return {
            "divergence": has_divergence,
            "climax_volume": has_climax,
            "signals": signals,
        }
