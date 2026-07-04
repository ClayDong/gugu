"""趋势跟踪策略：海龟、双均线、MACD。"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class TurtleStrategy(Strategy):
    """海龟交易系统：唐奇安通道突破 + ATR 动态止损。"""

    name = "turtle"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        bw = int(self.params["breakout_window"])
        ew = int(self.params["exit_window"])
        aw = int(self.params["atr_window"])

        df["upper"] = df["high"].rolling(bw).max().shift(1)
        df["lower"] = df["low"].rolling(bw).min().shift(1)
        df["exit_low"] = df["low"].rolling(ew).min().shift(1)
        df["atr"] = self._atr(df, aw)

        # 成交量均线：用于《炒股的智慧》阻力突破买点的成交量确认
        # 要求当日成交量 ≥ 近 20 日均量的 1.5 倍
        df["vol_ma"] = df["volume"].rolling(20).mean()

        df["signal"] = 0
        # 突破上轨买入
        breakout = df["close"] > df["upper"]
        if self.params.get("volume_confirm", True):
            # 根据《炒股的智慧》策略体系：阻力突破需成交量放大配合
            vol_ok = df["volume"] > df["vol_ma"] * float(self.params.get("vol_ratio", 1.5))
            df.loc[breakout & vol_ok, "signal"] = 1
        else:
            df.loc[breakout, "signal"] = 1
        # 跌破出场线卖出（卖出不需要量配合）
        df.loc[df["close"] < df["exit_low"], "signal"] = -1

        # 置信度：买入用突破上轨幅度，卖出用跌破出场线幅度
        atr_safe = df["atr"].replace(0, 1)
        buy_conf = (df["close"] - df["upper"]).clip(lower=0) / atr_safe
        sell_conf = (df["exit_low"] - df["close"]).clip(lower=0) / atr_safe
        df["confidence"] = (buy_conf + sell_conf).clip(0, 1)
        return df


class DualMAStrategy(Strategy):
    """双均线交叉：短均线上穿长均线买入，下穿卖出。

    P2-3 修复（炒股专家审查）：
    - 加入成交量确认（金叉当日量比 ≥ 1.5）避免震荡市假信号
    - 加入趋势强度过滤（MA20 斜率 > 0 + MA20 > MA60）避免下跌市金叉
    - A 股 70% 时间震荡，原逻辑频繁假信号是亏损机器
    - volume_confirm / trend_filter 可通过 params 关闭（用于回测对比）
    """

    name = "dual_ma"

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        sw = int(self.params["short_window"])
        lw = int(self.params["long_window"])

        df["ma_short"] = df["close"].rolling(sw).mean()
        df["ma_long"] = df["close"].rolling(lw).mean()

        df["signal"] = 0
        golden = (df["ma_short"] > df["ma_long"]) & (
            df["ma_short"].shift(1) <= df["ma_long"].shift(1)
        )

        # P2-3：成交量确认 + 趋势强度过滤（可通过 params 关闭）
        use_vol = self.params.get("volume_confirm", True)
        use_trend = self.params.get("trend_filter", True)

        buy_mask = golden
        if use_vol or use_trend:
            if use_vol:
                df["vol_ma"] = df["volume"].rolling(20).mean()
                vol_ratio = df["volume"] / df["vol_ma"].replace(0, 1)
                buy_mask = buy_mask & (vol_ratio >= float(self.params.get("vol_ratio", 1.5)))
            if use_trend:
                ma20 = df["close"].rolling(20).mean()
                ma20_slope = ma20 - ma20.shift(5)
                ma60 = df["close"].rolling(60).mean()
                trend_up = (ma20 > ma60) & (ma20_slope > 0)
                buy_mask = buy_mask & trend_up

        df.loc[buy_mask, "signal"] = 1
        # 死叉卖出（不需要量配合）
        death = (df["ma_short"] < df["ma_long"]) & (
            df["ma_short"].shift(1) >= df["ma_long"].shift(1)
        )
        df.loc[death, "signal"] = -1

        # 置信度：交叉信号本身是强信号，用交叉斜率衡量确定性
        # 金叉/死叉瞬间差值极小，用差值的变化率（斜率）更合理
        ma_diff = df["ma_short"] - df["ma_long"]
        ma_slope = ma_diff.diff().abs()
        price_safe = df["close"].replace(0, 1)
        # 基线 0.65 + 斜率贡献（最大 0.35），确保交叉信号置信度 ≥ 0.65
        df["confidence"] = (0.65 + (ma_slope / price_safe * 50).clip(0, 0.35)).clip(0, 1)
        # 无信号时置信度为 0
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        return df


class MACDStrategy(Strategy):
    """MACD 策略：金叉买入，死叉卖出，并加入顶背离/底背离检测作为独立卖出信号。"""

    name = "macd"

    def _detect_divergence(
        self, df: pd.DataFrame, n: int = 20
    ) -> pd.Series:
        """检测 MACD 顶背离和底背离。

        顶背离：价格创新高但 MACD DIF 值未创新高 → 强卖出信号。
        底背离：价格创新低但 MACD DIF 值未创新低 → 买入参考信号。

        Args:
            df: 含高、低、macd（DIF）列的 DataFrame
            n: 背离检测窗口（默认 20 个交易日）

        Returns:
            pd.Series，值为 1（顶背离卖出）、-1（底背离买入参考）、0（无信号）
        """
        half = n // 2
        high = df["high"]
        low = df["low"]
        dif = df["macd"]

        # 最近 half 日的最高价 / 最低价
        recent_high = high.rolling(half).max()
        recent_low = low.rolling(half).min()

        # 之前 half 日的最高价 / 最低价（shift half 以对齐两个时间段）
        prior_high = high.rolling(half).max().shift(half)
        prior_low = low.rolling(half).min().shift(half)

        # 之前 half 日 DIF 的最大值 / 最小值
        prior_dif_max = dif.rolling(half).max().shift(half)
        prior_dif_min = dif.rolling(half).min().shift(half)

        divergence = pd.Series(0, index=df.index)

        # 顶背离：价格创新高（当前 half 最高 > 前 half 最高），但 DIF 未创新高
        top_div = (recent_high > prior_high) & (dif < prior_dif_max)
        divergence.loc[top_div] = 1

        # 底背离：价格创新低（当前 half 最低 < 前 half 最低），但 DIF 未创新低
        bot_div = (recent_low < prior_low) & (dif > prior_dif_min)
        divergence.loc[bot_div] = -1

        return divergence

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        sw = int(self.params["short_window"])
        lw = int(self.params["long_window"])
        sig_w = int(self.params["signal_window"])

        ema_short = df["close"].ewm(span=sw, adjust=False).mean()
        ema_long = df["close"].ewm(span=lw, adjust=False).mean()
        df["macd"] = ema_short - ema_long
        df["macd_signal"] = df["macd"].ewm(span=sig_w, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]

        df["signal"] = 0
        golden = (df["macd"] > df["macd_signal"]) & (
            df["macd"].shift(1) <= df["macd_signal"].shift(1)
        )
        df.loc[golden, "signal"] = 1
        death = (df["macd"] < df["macd_signal"]) & (
            df["macd"].shift(1) >= df["macd_signal"].shift(1)
        )
        df.loc[death, "signal"] = -1

        # ── 背离检测 ──────────────────────────────────────────────
        n = int(self.params.get("divergence_window", 20))
        div = self._detect_divergence(df, n)

        # 顶背离：强卖出信号，覆盖买入信号（卖出优先）
        top_div_mask = div == 1
        df.loc[top_div_mask, "signal"] = -1

        # 底背离：仅当无已有信号时，作为买入参考
        bot_div_mask = (div == -1) & (df["signal"] == 0)
        df.loc[bot_div_mask, "signal"] = 1

        # ── 置信度 ──────────────────────────────────────────────
        # 交叉信号置信度
        hist_slope = df["macd_hist"].diff().abs()
        price_safe = df["close"].replace(0, 1)
        df["confidence"] = (0.65 + (hist_slope / price_safe * 50).clip(0, 0.35)).clip(0, 1)

        # 顶背离置信度设为 0.7（高于金叉死叉的基线）
        df.loc[top_div_mask, "confidence"] = 0.7

        # 无信号时置信度为 0
        df.loc[df["signal"] == 0, "confidence"] = 0.0
        return df
