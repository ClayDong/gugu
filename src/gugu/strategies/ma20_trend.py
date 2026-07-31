"""20日均线趋势跟随战法（AI量化决策系统）。

核心逻辑：
1. 流动性过滤：仅分析日成交额大于5亿元的个股
2. 趋势优先：仅筛选MA20方向向上的个股
3. 回踩低吸：股价回踩MA20附近+缩量30%+止跌K线
4. 放量突破：股价放量1.5倍突破MA20+收盘站稳
5. 止盈离场：乖离率>30%触发高抛/清仓
6. 止损离场：收盘价有效跌破MA20+放量跌破
7. 高位滞涨：放量长上影线或巨阴线+未创新高→减仓
8. 仓位管理：进攻/防守/空头三种模式
9. 拒绝摊平亏损：跌破MA20直接止损
"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class MA20TrendStrategy(Strategy):
    """20日均线趋势跟随战法。

    核心理念：
    - 只做MA20向上的个股，趋势为王
    - 回踩低吸为首选买点，放量突破为次选买点
    - 乖离率>30%无条件止盈，跌破MA20无条件止损
    - 拒绝摊平亏损，机械执行
    """

    name = "ma20_trend"
    category = "trend"

    def validate_params(self) -> None:
        """校验参数语义。"""
        ma_window = int(self.params.get("ma_window", 20))
        if ma_window < 5:
            raise ValueError("ma_window 应 >= 5")
        vol_shrink_pct = float(self.params.get("vol_shrink_pct", 0.30))
        if not 0 <= vol_shrink_pct <= 1:
            raise ValueError("vol_shrink_pct 应在 0-1 之间")
        vol_expand_ratio = float(self.params.get("vol_expand_ratio", 1.5))
        if vol_expand_ratio < 1:
            raise ValueError("vol_expand_ratio 应 >= 1")
        deviation_threshold = float(self.params.get("deviation_threshold", 0.30))
        if deviation_threshold < 0.1:
            raise ValueError("deviation_threshold 应 >= 0.1")
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.05))
        if not 0 < stop_loss_pct <= 0.2:
            raise ValueError("stop_loss_pct 应在 0-0.2 之间")
        min_daily_amount = float(self.params.get("min_daily_amount", 5e8))
        if min_daily_amount < 1e8:
            raise ValueError("min_daily_amount 应 >= 1亿")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        """生成20日均线趋势跟随信号。"""
        self._ensure_columns(df)
        df = df.copy()

        # ── 参数 ──────────────────────────────────────
        ma_window = int(self.params.get("ma_window", 20))
        vol_shrink_pct = float(self.params.get("vol_shrink_pct", 0.30))
        vol_expand_ratio = float(self.params.get("vol_expand_ratio", 1.5))
        deviation_threshold = float(self.params.get("deviation_threshold", 0.30))
        stop_loss_pct = float(self.params.get("stop_loss_pct", 0.05))
        vol_ma_window = int(self.params.get("vol_ma_window", 5))
        divergence_lookback = int(self.params.get("divergence_lookback", 20))
        shadow_ratio = float(self.params.get("shadow_ratio", 0.6))

        # ── 基础指标 ──────────────────────────────────────
        df["ma20"] = df["close"].rolling(ma_window, min_periods=ma_window).mean()
        df["ma20_slope"] = df["ma20"] - df["ma20"].shift(5)
        df["vol_ma"] = df["volume"].rolling(vol_ma_window, min_periods=1).mean()
        df["vol_ratio"] = df["volume"] / df["vol_ma"].replace(0, 1)

        # 乖离率 = (现价 - MA20) / MA20
        ma20_safe = df["ma20"].replace(0, 1)
        df["deviation"] = (df["close"] - ma20_safe) / ma20_safe

        # ── 趋势判定（互斥） ──────────────────────────────────────
        # 多头：股价 > MA20 且 MA20 上翘
        df["trend_bull"] = (df["close"] > df["ma20"]) & (df["ma20_slope"] > 0)
        # 震荡：股价围绕MA20波动，MA20走平（斜率接近零，排除已判定为多头的）
        df["trend_sideways"] = (
            df["close"].between(df["ma20"] * 0.95, df["ma20"] * 1.05)
            & (df["ma20_slope"].abs() < df["ma20"] * 0.002)
            & ~df["trend_bull"]
        )
        # 空头：股价 < MA20 且 MA20 下弯（排除已判定为震荡的）
        df["trend_bear"] = (
            (df["close"] < df["ma20"]) & (df["ma20_slope"] < 0) & ~df["trend_sideways"]
        )

        # ── K线形态识别 ──────────────────────────────────────
        # 实体大小
        df["body"] = (df["close"] - df["open"]).abs()
        df["body_pct"] = df["body"] / df["close"].replace(0, 1)
        # 上影线
        df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["close"].replace(
            0, 1
        )
        # 下影线
        df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"]) / df["close"].replace(
            0, 1
        )
        # 十字星：实体 < 20% 且 上下影线都较长
        df["doji"] = (
            (df["body_pct"] < 0.02) & (df["upper_shadow"] > 0.02) & (df["lower_shadow"] > 0.02)
        )
        # 小阳线：实体 1-3% 且 收盘价 > 开盘价
        df["small_bull"] = (
            (df["body_pct"] > 0.01) & (df["body_pct"] < 0.03) & (df["close"] > df["open"])
        )
        # 长下影线：下影线 > 实体 * 2
        df["long_lower_shadow"] = df["lower_shadow"] > (df["body_pct"] * 2 + 0.02)
        # 止跌信号：十字星 or 小阳线 or 长下影线
        df["reversal_signal"] = df["doji"] | df["small_bull"] | df["long_lower_shadow"]

        # ── 买入信号 ──────────────────────────────────────
        # 信号A：回踩低吸（首选）
        # 条件：股价在MA20附近（乖离率<2%）+ 缩量30%以上 + 止跌K线
        near_ma20 = df["deviation"].abs() < 0.02
        vol_shrink = df["vol_ratio"] < (1 - vol_shrink_pct)
        df["signal_a_buy"] = df["trend_bull"] & near_ma20 & vol_shrink & df["reversal_signal"]

        # 信号B：放量突破
        # 条件：放量1.5倍 + 突破MA20 + 收盘站稳
        vol_expand = df["vol_ratio"] >= vol_expand_ratio
        breakout_ma20 = (df["close"] > df["ma20"]) & (df["close"].shift(1) <= df["ma20"].shift(1))
        df["signal_b_buy"] = vol_expand & breakout_ma20

        # 综合买入信号
        df["signal"] = 0
        df.loc[df["signal_a_buy"] | df["signal_b_buy"], "signal"] = 1

        # ── 卖出信号 ──────────────────────────────────────
        # 信号C：止盈离场（乖离率>30%）
        df["signal_c_sell"] = df["deviation"] > deviation_threshold

        # 信号D：止损离场（有效跌破MA20 + 放量）
        # 有效跌破：收盘价 < MA20 且 放量跌破
        below_ma20 = df["close"] < df["ma20"]
        vol_breakout = df["vol_ratio"] > 1.2
        df["signal_d_sell"] = below_ma20 & vol_breakout

        # 信号E：高位滞涨（放量长上影线或巨阴线 + 未创新高）
        # 长上影线：上影线 > 实体 * shadow_ratio
        long_upper_shadow = df["upper_shadow"] > (df["body_pct"] * shadow_ratio + 0.03)
        # 巨阴线：实体 > 5% 且 收盘价 < 开盘价
        big_bear = (df["body_pct"] > 0.05) & (df["close"] < df["open"])
        # 未创新高：近divergence_lookback日高点未突破
        recent_high = df["high"].rolling(divergence_lookback).max().shift(1)
        not_new_high = df["high"] < recent_high
        df["signal_e_sell"] = (
            (long_upper_shadow | big_bear) & not_new_high & (df["vol_ratio"] > 1.0)
        )

        # 综合卖出信号（优先级：止损 > 止盈 > 滞涨）
        df.loc[df["signal_d_sell"], "signal"] = -1
        df.loc[df["signal_c_sell"] & (df["signal"] != -1), "signal"] = -1
        df.loc[df["signal_e_sell"] & (df["signal"] == 0), "signal"] = -1

        # ── 置信度计算 ──────────────────────────────────────
        # 买入置信度
        buy_confidence = pd.Series(0.0, index=df.index)
        # 信号A（回踩低吸）：基础0.7 + 趋势强度 + 缩量程度
        trend_strength = (df["ma20_slope"] / ma20_safe).clip(0, 0.01) * 100
        vol_shrink_degree = (1 - df["vol_ratio"]).clip(0, 0.5) * 0.4
        buy_confidence[df["signal_a_buy"]] = (0.7 + trend_strength + vol_shrink_degree).clip(
            0.6, 0.95
        )[df["signal_a_buy"]]
        # 信号B（放量突破）：基础0.65 + 突破力度
        breakout_strength = ((df["close"] - df["ma20"]) / ma20_safe).clip(0, 0.05) * 5
        buy_confidence[df["signal_b_buy"]] = (0.65 + breakout_strength).clip(0.6, 0.90)[
            df["signal_b_buy"]
        ]

        # 卖出置信度
        sell_confidence = pd.Series(0.0, index=df.index)
        # 信号D（止损）：基础0.9（铁律）
        sell_confidence[df["signal_d_sell"]] = 0.9
        # 信号C（止盈）：基础0.7 + 乖离率程度
        deviation_degree = (df["deviation"] - deviation_threshold).clip(0, 0.2) * 2
        sell_confidence[df["signal_c_sell"]] = (0.7 + deviation_degree).clip(0.7, 0.95)[
            df["signal_c_sell"]
        ]
        # 信号E（滞涨）：基础0.6
        sell_confidence[df["signal_e_sell"]] = 0.6

        # 合并置信度
        df["confidence"] = 0.0
        df.loc[df["signal"] == 1, "confidence"] = buy_confidence
        df.loc[df["signal"] == -1, "confidence"] = sell_confidence

        # ── 仓位建议 ──────────────────────────────────────
        # 根据趋势状态给出仓位系数
        df["position_ratio"] = 0.0
        # 进攻模式（多头）：5-7成
        df.loc[df["trend_bull"], "position_ratio"] = 0.6
        # 防守模式（震荡）：3-5成
        df.loc[df["trend_sideways"], "position_ratio"] = 0.4
        # 空头模式：0-2成
        df.loc[df["trend_bear"], "position_ratio"] = 0.1

        # ── 止损止盈参考价 ──────────────────────────────────────
        df["stop_loss_price"] = df["ma20"] * (1 - stop_loss_pct)
        df["take_profit_price"] = df["ma20"] * (1 + deviation_threshold)

        return df
