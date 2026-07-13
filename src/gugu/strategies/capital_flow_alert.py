"""资金流异动策略：基于 OHLCV 近似资金流特征的主力撤离 / 洗盘结束信号。

来源：豆包AI「龙头股散户化」分析 + 实战派资金流监控

第一性原理：
- 资金出逃早于股价大跌，4 特征同时出现 = 主力撤离
- 主力洗盘结束时缩量回踩不破支撑 + 中期趋势向上 + 前期有强势表现

═══════════════════════════════════════════════════════════
卖出信号（4 核心特征，任一触发 → signal=-1）
═══════════════════════════════════════════════════════════
1. 持续放量下跌（3 日内 2 日放量且 3 日累计收跌）
2. 量价背离（放量但收盘走弱，前一日还是涨）
3. 跌破 MA20 支撑
4. 高位巨量滞涨（放量 + 长上影 + 涨幅小，主力出货）

置信度评分：
- 1 特征触发：0.5
- 2 特征同时触发：0.7
- 3+ 特征同时触发：0.85

═══════════════════════════════════════════════════════════
买入信号（3 核心特征，同时满足 → signal=1）
═══════════════════════════════════════════════════════════
1. 缩量回踩不破 MA20（主力洗盘结束）
2. MA20 向上拐头（5 日前 vs 当前）
3. 前期有强势表现（5 日内最高涨幅 ≥ 5%）

置信度评分：
- 基础 0.5（满足 3 条件）
- +0.15 缩量程度（vol_ratio < 0.6 时加 0.15）
- +0.10 MA20 斜率强度（ma20_slope / ma20 > 0.01 时加 0.10）
- +0.10 距离 MA20 距离（(close - ma20) / ma20 在 [0, 0.02] 时加 0.10）
- 上限 0.85

═══════════════════════════════════════════════════════════
设计约束
═══════════════════════════════════════════════════════════
- 策略本身只用 OHLCV 数据生成信号（与 SignalRouter 兼容，回测可复现）
- 真正的资金流判断在 SignalPipeline 步骤 2 由 MoneyFlowFilter 完成
- 策略只生成基础信号，不调用外部资金流 API

信号赋值规则：
- 卖出优先于买入（与 double_yin 一致）
- signal 列：1=买，-1=卖，0=持有
- confidence 列：0-1
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class CapitalFlowAlertStrategy(Strategy):
    """资金流异动监控策略：4卖出特征识别主力出逃 + 3买入特征捕捉洗盘结束。

    第一性原理：资金出逃早于股价大跌，4 特征同时出现 = 主力撤离。
    策略只用 OHLCV 近似资金流特征，真正的资金流判断在 SignalPipeline
    步骤 2 由 MoneyFlowFilter 完成（保持策略无外部 IO）。
    """

    name = "capital_flow_alert"

    def validate_params(self) -> None:
        """校验参数语义。"""
        if self.params.get("ma20_window", 20) <= 0:
            raise ValueError("ma20_window 必须 > 0")
        if self.params.get("vol_ma_window", 5) <= 0:
            raise ValueError("vol_ma_window 必须 > 0")
        if self.params.get("shrink_ratio", 0.8) >= self.params.get("breakout_volume_ratio", 1.3):
            raise ValueError("shrink_ratio 必须 < breakout_volume_ratio")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成资金流异动信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        ma20_window = int(self.params.get("ma20_window", 20))
        vol_ma_window = int(self.params.get("vol_ma_window", 5))
        shrink_ratio = float(self.params.get("shrink_ratio", 0.8))
        _breakout_volume_ratio = float(self.params.get("breakout_volume_ratio", 1.3))
        stagnation_volume_ratio = float(self.params.get("stagnation_volume_ratio", 2.0))
        pullback_max_pct = float(self.params.get("pullback_max_pct", -0.02))
        recent_strong_window = int(self.params.get("recent_strong_window", 5))
        recent_strong_threshold = float(self.params.get("recent_strong_threshold", 0.05))

        # ── 计算均线 ──────────────────────────────────
        df["ma20"] = df["close"].rolling(ma20_window).mean()

        # MA20 斜率（5 日前 vs 当前，向上拐头判定）
        df["ma20_slope"] = df["ma20"] - df["ma20"].shift(recent_strong_window)

        # ── 计算量能指标 ──────────────────────────────
        df["vol_ma5"] = df["volume"].rolling(vol_ma_window).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # ── K线基础属性 ────────────────────────────────
        df["daily_return"] = df["close"].pct_change()
        df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["open"].replace(
            0, np.nan
        )

        # ════════════════════════════════════════════════
        # 卖出信号：4 核心特征（任一触发 → signal=-1）
        # ════════════════════════════════════════════════
        # 特征 1：持续放量下跌（3 日内 2 日放量且 3 日累计收跌）
        cond_volume_drop = ((df["volume"] > df["vol_ma5"] * 1.2).rolling(3).sum() >= 2) & (
            df["close"].pct_change(3) < -0.03
        )

        # 特征 2：量价背离（放量但收盘走弱，前一日还是涨）
        cond_divergence = (
            (df["volume"] > df["vol_ma5"] * 1.3)
            & (df["close"] < df["close"].shift(1))
            & (df["close"].shift(1) > df["close"].shift(2))  # 前一日还是涨
        )

        # 特征 3：跌破 MA20 支撑
        cond_break_ma20 = df["close"] < df["ma20"]

        # 特征 4：高位巨量滞涨（主力出货）
        # P1-3 修复：原 abs(daily_return) < 0.02 错误包含大跌（-1.5% 也算"滞涨"），
        # 实际应排除大跌（滞涨定义为"价格几乎不动"，大跌是另一种卖出信号）
        cond_stagnation = (
            (df["volume"] > df["vol_ma5"] * stagnation_volume_ratio)
            & (df["upper_shadow"] > 0.02)
            & (df["daily_return"] >= -0.005)
            & (df["daily_return"] <= 0.02)
        )

        # 卖出特征计数（1/2/3+ 决定置信度）
        sell_features = (
            cond_volume_drop.astype(int)
            + cond_divergence.astype(int)
            + cond_break_ma20.astype(int)
            + cond_stagnation.astype(int)
        )
        sell_cond = sell_features >= 1

        # ════════════════════════════════════════════════
        # 买入信号：3 核心特征（同时满足 → signal=1）
        # ════════════════════════════════════════════════
        # 特征 1：缩量回踩不破 MA20（主力洗盘结束）
        cond_shrink_pullback = (
            (df["volume"] < df["vol_ma5"] * shrink_ratio)
            & (df["close"] >= df["ma20"])
            & (df["daily_return"] > pullback_max_pct)
        )

        # 特征 2：MA20 向上拐头（5 日前 vs 当前）
        cond_ma20_up = df["ma20"] > df["ma20"].shift(recent_strong_window)

        # 特征 3：前期有强势表现（5 日内最高涨幅 ≥ 5%）
        df["recent_max"] = df["close"].rolling(recent_strong_window).max()
        df["recent_min"] = df["close"].rolling(recent_strong_window).min()
        cond_recent_strong = (
            df["recent_max"] / df["recent_min"].shift(recent_strong_window) - 1
        ) >= recent_strong_threshold

        buy_cond = cond_shrink_pullback & cond_ma20_up & cond_recent_strong

        # ════════════════════════════════════════════════
        # 信号赋值（卖出优先于买入）
        # ════════════════════════════════════════════════
        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        # 卖出信号优先（任一卖出特征触发即撤离）
        df.loc[sell_cond, "signal"] = -1

        # ════════════════════════════════════════════════
        # 置信度赋值
        # ════════════════════════════════════════════════
        df["confidence"] = 0.0

        # 卖出置信度：1 特征 0.5 / 2 特征 0.7 / 3+ 特征 0.85
        df.loc[sell_features >= 1, "confidence"] = 0.5
        df.loc[sell_features >= 2, "confidence"] = 0.7
        df.loc[sell_features >= 3, "confidence"] = 0.85

        # 买入置信度：基础 0.5 + 缩量/斜率/距离加分（上限 0.85）
        # 仅对纯买入信号（非卖出当日）赋值
        buy_only = buy_cond & ~sell_cond
        buy_confidence = pd.Series(0.5, index=df.index)  # 基础分

        # +0.15 缩量程度（vol_ratio < 0.6 时加 0.15）
        buy_confidence = buy_confidence + ((df["vol_ratio"] < 0.6).astype(float) * 0.15)

        # +0.10 MA20 斜率强度（ma20_slope / ma20 > 0.01 时加 0.10）
        ma20_slope_ratio = df["ma20_slope"] / df["ma20"].replace(0, np.nan)
        buy_confidence = buy_confidence + ((ma20_slope_ratio > 0.01).astype(float) * 0.10)

        # +0.10 距离 MA20 距离（(close - ma20) / ma20 在 [0, 0.02] 时加 0.10）
        ma20_distance = (df["close"] - df["ma20"]) / df["ma20"].replace(0, np.nan)
        buy_confidence = buy_confidence + (
            ((ma20_distance >= 0) & (ma20_distance <= 0.02)).astype(float) * 0.10
        )

        # 上限 0.85
        buy_confidence = buy_confidence.clip(upper=0.85)
        df.loc[buy_only, "confidence"] = buy_confidence[buy_only]

        return df
