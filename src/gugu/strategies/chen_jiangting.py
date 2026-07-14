"""陈江挺《炒股的智慧》选股策略 — 阻力突破 + 量能确认 + 阶段判断。

核心思想（陈江挺《炒股的智慧》第7-9章）：
1. 选股先看大市：大市不满足顺势条件时不选股（由 MarketFilter 在 engine/main.py 拦截）
2. 阻力突破：股价突破前期高点（阻力线）时进场，必须伴随成交量放大
3. 量能确认：突破当日成交量 ≥ N 日均量的 volume_ratio 倍
4. 阶段判断：只在"正常升势"阶段买入，"疯狂"和"末升期"不追
5. 不抄底：避免在下跌趋势中"逢低吸纳"，只做趋势确认后的突破

与 money_flow_follow 的区别：
- money_flow_follow 强调"创新高回踩后放量"（回踩再突破）
- chen_jiangting 强调"直接突破阻力 + 量能配合"（不要求回踩）
- 两者互补，覆盖不同形态

卖出条件：
- 跌破 20 日均线（趋势破坏）
- 量价背离：价格创新高但成交量 < 5 日均量
"""

from __future__ import annotations

import pandas as pd

from gugu.strategies.base import Strategy


class ChenJiangtingStrategy(Strategy):
    """陈江挺选股策略：阻力突破 + 量能确认 + 阶段判断。

    买入条件（全部满足）：
    1. 阻力突破：当日收盘价突破近 resistance_window 日的最高价
    2. 量能确认：当日成交量 ≥ vol_ma_window 日均量的 volume_ratio 倍
    3. 趋势向上：MA20 > MA60（中期趋势向上）
    4. 非ST非停牌：由上游过滤（_filter_basic + is_tradable）
    5. 阶段过滤：当日涨幅 < max_daily_gain_pct（排除已疯涨的"疯狂"阶段）

    卖出条件（任一满足）：
    - 跌破 MA20（趋势破坏）
    - 量价背离：价格创 20 日新高但成交量 < 5 日均量
    """

    name = "chen_jiangting"
    category = "trend"

    def validate_params(self) -> None:
        rw = int(self.params.get("resistance_window", 20))
        if rw < 5:
            raise ValueError("resistance_window 必须 >= 5")
        vr = float(self.params.get("volume_ratio", 1.5))
        if vr < 1.0:
            raise ValueError("volume_ratio 必须 >= 1.0")

    def _generate_signals_impl(self, df: pd.DataFrame) -> pd.DataFrame:
        self._ensure_columns(df)
        df = df.copy()

        resistance_window = int(self.params.get("resistance_window", 20))
        volume_ratio = float(self.params.get("volume_ratio", 1.5))
        vol_ma_window = int(self.params.get("vol_ma_window", 20))
        ma_short = int(self.params.get("ma_short", 20))
        ma_long = int(self.params.get("ma_long", 60))
        max_daily_gain_pct = float(self.params.get("max_daily_gain_pct", 0.07))
        divergence_vol_window = int(self.params.get("divergence_vol_window", 5))

        # 数据长度校验
        min_len = max(resistance_window, ma_long, vol_ma_window) + 2
        if len(df) < min_len:
            df["signal"] = 0
            df["confidence"] = 0.0
            return df

        # ── 计算均线 ──
        df["ma_short"] = df["close"].rolling(ma_short).mean()
        df["ma_long"] = df["close"].rolling(ma_long).mean()
        vol_ma = df["volume"].rolling(vol_ma_window).mean()

        # ── 条件1：阻力突破 ──
        # 近 resistance_window 日最高价（不含当日）
        resistance = df["high"].rolling(resistance_window).max().shift(1)
        breakout = df["close"] > resistance

        # ── 条件2：量能确认 ──
        vol_confirm = df["volume"] >= vol_ma * volume_ratio

        # ── 条件3：中期趋势向上 ──
        trend_up = df["ma_short"] > df["ma_long"]

        # ── 条件4：阶段过滤（排除已疯涨）──
        daily_gain = df["close"] / df["close"].shift(1) - 1.0
        not_crazy = daily_gain < max_daily_gain_pct

        # ── 买入信号：4 条件全部满足 ──
        buy_cond = breakout & vol_confirm & trend_up & not_crazy

        # ── 卖出信号1：跌破 MA20 ──
        below_ma = df["close"] < df["ma_short"]

        # ── 卖出信号2：量价背离（价格创 20 日新高但成交量萎缩）──
        price_new_high = df["close"] >= df["close"].rolling(20).max().shift(1)
        vol_shrink = df["volume"] < df["volume"].rolling(divergence_vol_window).mean()
        divergence = price_new_high & vol_shrink

        sell_cond = below_ma | divergence

        df["signal"] = 0
        df.loc[buy_cond, "signal"] = 1
        # 卖出覆盖买入（趋势破坏时不入场）
        df.loc[sell_cond, "signal"] = -1

        # ── 置信度计算 ──
        # 突破幅度 + 量比 + 趋势强度 综合
        breakout_pct = (df["close"] - resistance) / resistance.where(resistance > 0, 1)
        vol_ratio_actual = (df["volume"] / vol_ma).where(vol_ma > 0, 0)
        trend_strength = (df["ma_short"] - df["ma_long"]) / df["ma_long"].where(
            df["ma_long"] > 0, 1
        )

        confidence = (
            (
                0.4 * breakout_pct.clip(0, 0.1) / 0.1  # 突破幅度 0-10% 映射 0-0.4
                + 0.3 * (vol_ratio_actual - 1.0).clip(0, 1)  # 量比超出部分映射 0-0.3
                + 0.3 * trend_strength.clip(0, 0.1) / 0.1  # 趋势强度 0-10% 映射 0-0.3
            )
            .fillna(0)
            .clip(0, 1.0)
        )

        df["confidence"] = confidence.where(buy_cond, 0.0)

        return df
