"""早盘假摔低吸战法：花木兰捕捉主力洗盘策略。

来源：战神花木兰系列文档整理

适用场景：个股上升趋势中的正常回调。

核心逻辑：
1. 信号：开盘半小时快速下杀，跌幅超3%，但10:30前收回跌幅，当天收红
2. 关键验证：下跌时成交量必须低于近5日均量（缩量跌是真洗盘，放量跌是真出货）
3. 买点：假摔后股价回踩支撑位（不破前低）时介入，不追高
4. 卖点：次日冲高或再次滞涨时卖出

与现有策略差异：
- breakout_rebound：涨停后断板反包（短线主力行为）
- huamulan_fake_dip：开盘假摔+缩量收回（洗盘识别）

回测说明：
- 假摔信号在日K线中表现为：长下影线 + 阳线 + 缩量
- 下影线长度 > 3%（盘中跌幅）
- 收盘价 > 开盘价（当天收红）
- 成交量 < 前5日均量（缩量）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from gugu.strategies.base import Strategy


class HuamulanFakeDipStrategy(Strategy):
    """早盘假摔低吸战法策略。

    捕捉主力洗盘机会，适合上升趋势中的回调买入。
    核心逻辑：缩量跌是真洗盘，放量跌是真出货。

    回测说明：
    - 日K线近似：长下影线（>3%）+ 阳线 + 缩量
    - 实盘中需分钟级数据精确判断"10:30前收回"
    """

    name = "huamulan_fake_dip"
    category = "mean_revert"

    def validate_params(self) -> None:
        """校验参数语义。"""
        shadow_min = float(self.params.get("lower_shadow_min_pct", 0.03))
        if shadow_min <= 0:
            raise ValueError("lower_shadow_min_pct 必须大于 0")
        vol_shrink = float(self.params.get("vol_shrink_ratio", 1.0))
        if vol_shrink <= 0:
            raise ValueError("vol_shrink_ratio 必须大于 0")
        stop_loss = float(self.params.get("stop_loss_pct", 0.05))
        if stop_loss <= 0:
            raise ValueError("stop_loss_pct 必须大于 0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成假摔低吸信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        lower_shadow_min_pct = float(self.params.get("lower_shadow_min_pct", 0.03))
        vol_shrink_ratio = float(self.params.get("vol_shrink_ratio", 1.0))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.05))
        take_profit_pct = float(self.params.get("take_profit_pct", 0.06))
        time_stop_window = int(self.params.get("time_stop_window", 5))
        time_stop_threshold = float(self.params.get("time_stop_threshold", 0.02))
        trend_ma_window = int(self.params.get("trend_ma_window", 20))

        # ── 计算均线系统 ──────────────────────────────
        df["ma5"] = df["close"].rolling(5).mean()
        df["ma10"] = df["close"].rolling(10).mean()
        df["ma20"] = df["close"].rolling(trend_ma_window).mean()

        # ── 计算量能指标 ──────────────────────────────
        df["vol_ma5"] = df["volume"].rolling(5).mean().shift(1)
        df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)

        # ── K线基础属性 ──────────────────────────────
        df["daily_return"] = df["close"].pct_change()
        df["is_yang"] = (df["close"] > df["open"]).astype(bool)
        df["is_yin"] = (df["close"] < df["open"]).astype(bool)

        # 下影线长度 = (min(open, close) - low) / open
        df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"]) / df["open"].replace(
            0, np.nan
        )

        # 上影线长度 = (high - max(open, close)) / open
        df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["open"].replace(
            0, np.nan
        )

        # 实体比例 = |close - open| / open
        df["body_ratio"] = (df["close"] - df["open"]).abs() / df["open"].replace(0, np.nan)

        # ════════════════════════════════════════════════
        # 买入条件（假摔低吸）
        # ════════════════════════════════════════════════
        # 条件1：长下影线（>3%，盘中跌幅超3%后收回）
        cond_long_lower = df["lower_shadow"] >= lower_shadow_min_pct

        # 条件2：当天收阳线（close > open，10:30前收回跌幅）
        cond_yang = df["is_yang"]

        # 条件3：缩量（成交量 < 前5日均量，缩量跌是洗盘）
        cond_shrink = df["vol_ratio"] < vol_shrink_ratio

        # 条件4：不破前低（股价回踩支撑位，不破前5日最低）
        df["prev_low"] = df["low"].rolling(5).min().shift(1)
        cond_support = df["low"] >= df["prev_low"]

        # 条件5：趋势向上（MA5 > MA10 > MA20）
        cond_trend = (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"])

        # 综合买入信号（5条件全满足）
        buy_cond = cond_long_lower & cond_yang & cond_shrink & cond_support & cond_trend

        # ════════════════════════════════════════════════
        # 卖出条件
        # ════════════════════════════════════════════════
        # 1. 止损：跌破入场价5%或跌破前低
        df["stop_loss_price"] = df["close"] * (1 - stop_loss_pct)
        cond_stop_loss = (df["close"] < df["stop_loss_price"]) | (df["low"] < df["prev_low"] * 0.97)

        # 2. 止盈：涨幅达6%或上影线过长（滞涨）
        df["take_profit_price"] = df["close"] * (1 + take_profit_pct)
        cond_take_profit = (df["close"] >= df["take_profit_price"]) | (df["upper_shadow"] > 0.05)

        # 3. 时间止损：持仓5日涨幅 < 2%（无进展）
        df["return_5d"] = df["close"].pct_change(periods=time_stop_window)
        cond_time_stop = df["return_5d"] < time_stop_threshold

        # 信号赋值（卖出优先于买入）
        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        df.loc[cond_stop_loss | cond_take_profit | cond_time_stop, "signal"] = -1

        # ── 置信度计算 ────────────────────────────────
        # 基于：下影线长度 + 缩量程度 + 趋势强度
        shadow_score = (df["lower_shadow"].clip(upper=0.08) / 0.08).clip(0, 1)
        shrink_score = (1 - df["vol_ratio"].clip(upper=1.0)).clip(0, 1)
        trend_score = (
            ((df["ma5"] - df["ma20"]) / df["ma20"].replace(0, np.nan)).clip(0, 0.1) / 0.1
        ).clip(0, 1)

        df["confidence"] = (
            (shadow_score * 0.4 + shrink_score * 0.3 + trend_score * 0.3).fillna(0.0).clip(0, 1)
        )

        # 无信号时置信度为0
        df.loc[df["signal"] == 0, "confidence"] = 0.0

        return df
